"""Official SEC proxy ownership-baseline acquisition and reconciliation.

Proxy tables are issuer-reported snapshots.  They are stored independently
from Form 3/4/5 positions because beneficial ownership, voting power, and
derivative rights are not generally additive to Section 16 holdings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from bs4 import BeautifulSoup

from .models import (
    InsiderOwnershipPosition,
    InsiderPerson,
    InsiderTransaction,
    OwnershipBaseline,
    OwnershipReconciliation,
    SECFiling,
)
from .sec_ingestion import SECSourceProvider
from .store import InsiderStore


_MISSING = {"", "*", "—", "–", "-", "�", "n/a", "na", "none"}
_ROLE_PATTERNS = (
    ("CEO", re.compile(r"\bchief executive officer\b|\bCEO\b|principal executive officer", re.I)),
    ("CFO", re.compile(r"\bchief financial officer\b|\bCFO\b|principal financial officer", re.I)),
    ("director", re.compile(r"\bdirector\b|chairman|chair of the board", re.I)),
    ("founder", re.compile(r"\bco[- ]?founder\b|\bfounder\b", re.I)),
    ("officer", re.compile(r"\bchief\b|\bpresident\b|\bsecretary\b|\btreasurer\b|\bvice president\b|\bexecutive vice president\b|\bgeneral counsel\b|\bexecutive officer\b", re.I)),
)


def _clean(value: str) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").replace("\ufffd", "�").split())


def _name_without_markers(value: str) -> str:
    value = _clean(value)
    value = re.sub(r"\s*\(\d+\)", "", value)
    # Institutional table rows often include an address after the legal name.
    if re.search(r"\b\d{1,6}\s+[A-Z][^,]{2,}\s+(?:New York|Malvern|San Francisco|Greenwich)", value):
        value = re.split(r"\b\d{1,6}\s+[A-Z]", value, maxsplit=1)[0].strip(" ,")
    return value.strip(" ,")


def _name_key(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKD", _name_without_markers(value)).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b(?:dr|mr|mrs|ms|miss|phd|jd|dvm|frs)\b", " ", value)
    noise = {"ph", "d", "md", "jd", "dvm", "frs", "jr", "sr", "ii", "iii", "iv", "gen", "lieut", "prof"}
    return tuple(sorted(token for token in re.findall(r"[a-z]+", value) if len(token) > 1 and token not in noise))


def _number(value: str) -> float | None:
    text = _clean(value).replace(",", "").replace("$", "")
    if text.lower() in _MISSING or text in {"��", "�"}:
        return None
    text = text.rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def _percent(value: str) -> tuple[float | None, str | None]:
    raw = _clean(value)
    if raw.lower() in _MISSING or raw in {"��", "�"}:
        return None, ("<1%" if raw == "*" else raw or None)
    number = _number(raw)
    return number, (raw if raw else None)


def _collapse_percent_tokens(values: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if index + 1 < len(values) and values[index + 1] == "%":
            result.append(value + "%")
            index += 2
        else:
            result.append(value)
            index += 1
    return result


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return digest


@dataclass
class ProxyBaselineResult:
    filing: SECFiling
    baselines: list[OwnershipBaseline]
    warnings: list[str]
    as_of_date: str | None
    section_context: str
    role_assertions: dict[str, list[str]]


def _extract_as_of(text: str) -> str | None:
    match = re.search(r"\bas of\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", text, re.I)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _role_assertions(soup: BeautifulSoup) -> dict[tuple[str, ...], list[str]]:
    result: dict[tuple[str, ...], set[str]] = {}
    for table in soup.find_all("table"):
        table_text = _clean(table.get_text(" ", strip=True))
        first_cells = [_clean(c.get_text(" ", strip=True)) for c in table.find_all("tr", limit=2)[0].find_all(["td", "th"])] if table.find_all("tr", limit=2) else []
        first_header = " ".join(first_cells).lower()
        table_is_director_roster = bool(re.search(r"^(?:director nominees|continuing directors|nominees for director|directors with)", first_header, re.I) and re.search(r"\bposition\b|director since", table_text, re.I) and "table of contents" not in table_text.lower())
        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            row = " | ".join(cells)
            roles = {label for label, pattern in _ROLE_PATTERNS if pattern.search(row)}
            if table_is_director_roster and len(cells) >= 2 and not re.search(r"director nominees|continuing directors|name|position", cells[0], re.I):
                roles.add("director")
            if re.search(r"\bformer\b", row, re.I):
                roles.difference_update({"CEO", "CFO", "officer"})
            if not roles:
                continue
            key = _name_key(cells[0])
            if key:
                result.setdefault(key, set()).update(roles)
    return {key: sorted(values) for key, values in result.items()}


def _roles_for_person(soup: BeautifulSoup, name: str) -> list[str]:
    """Find role assertions in biography rows where SEC HTML nests prose."""
    key = set(_name_key(name))
    if not key:
        return []
    roles: set[str] = set()
    for tr in soup.find_all("tr"):
        row = _clean(tr.get_text(" ", strip=True))
        row_key = set(_name_key(row))
        if len(key.intersection(row_key)) < (1 if len(key) == 1 else 2):
            continue
        for label, pattern in _ROLE_PATTERNS:
            if pattern.search(row):
                # Biography prose often mentions a past employer's CEO/CFO
                # title.  Only founder/director assertions are safe to infer
                # from free-form prose; current officer titles come from
                # structured position tables handled by _role_assertions.
                if label == "founder":
                    roles.add(label)
        if "director since" in row.lower():
            roles.add("director")
        first_exec = min((position for position in (row.lower().find("chief executive"), row.lower().find("chief financial"), row.lower().find("executive officer")) if position >= 0), default=10**9)
        prior_role = row.lower().find("previously")
        if re.search(r"\bformer\b", row, re.I) or (prior_role >= 0 and prior_role < first_exec):
            roles.difference_update({"CEO", "CFO", "officer"})
    return sorted(roles)


def _ownership_table(soup: BeautifulSoup, ticker: str):
    candidates = []
    for table in soup.find_all("table"):
        text = _clean(table.get_text(" ", strip=True)).lower()
        if "beneficial" not in text:
            continue
        rows = []
        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                rows.append(cells)
        if rows and any("name of beneficial" in " ".join(row).lower() or "shares beneficially owned" in " ".join(row).lower() for row in rows):
            candidates.append((table, rows))
    if not candidates:
        raise ValueError(f"No beneficial ownership table found in {ticker} proxy")
    # EXEL splits 5% holders into a second table; the first is the insider table.
    return candidates


def _class_schema(ticker: str) -> tuple[list[str], int]:
    ticker = ticker.upper()
    if ticker == "ZM":
        return ["Class A Common Stock", "Class B Common Stock"], 5
    if ticker == "PLTR":
        return ["Class A", "Class B", "Class F"], 7
    return ["Common Stock"], 2


def _extract_footnote_texts(section_context: str, markers: list[str]) -> list[str]:
    lower = section_context.lower()
    starts = [position for position in (lower.find("this table is based"), lower.find("this table is based upon")) if position >= 0]
    if not starts:
        return markers
    block = section_context[min(starts):]
    result: list[str] = []
    for marker in markers:
        match = re.search(re.escape(marker) + r"\s*(.+?)(?=\s+\(\d+\)|$)", block, re.S)
        if match:
            cleaned = re.sub(r"\s+", " ", match.group(1)).strip()
            result.append(f"{marker} {cleaned[:600]}")
        else:
            result.append(marker)
    return result


def _acquirable_within_60_days(text: str, name: str) -> float | None:
    keys = _name_key(name)
    surname = keys[-1] if keys else ""
    if not surname:
        return None
    for match in re.finditer(re.escape(surname), text, re.I):
        window = text[match.start(): match.start() + 700]
        if not re.search(r"60\s+days", window, re.I):
            continue
        number = re.search(r"([0-9][0-9,]*)\s+shares?", window, re.I)
        if number:
            return _number(number.group(1))
    return None


def parse_proxy_html(
    html: str | bytes,
    filing: SECFiling,
    *,
    existing_people: Sequence[InsiderPerson] = (),
) -> ProxyBaselineResult:
    """Parse the issuer's beneficial-ownership table without using rendered search results."""
    soup = BeautifulSoup(html, "html.parser")
    ticker = filing.ticker.upper()
    text = _clean(soup.get_text(" ", strip=True))
    tables = _ownership_table(soup, ticker)
    table_text = _clean(tables[0][0].get_text(" ", strip=True))
    table_position = text.find(table_text[:120]) if table_text else -1
    heading_positions = [match.start() for match in re.finditer(r"security ownership of certain beneficial owners and management", text, re.I)]
    prior_headings = [position for position in heading_positions if table_position < 0 or position < table_position]
    section_start = max(prior_headings) if prior_headings else (heading_positions[-1] if heading_positions else 0)
    marker = re.search(r"security ownership of certain beneficial owners and management", text[section_start:], re.I)
    section_context = text[section_start: section_start + 6000]
    as_of = _extract_as_of(section_context) or _extract_as_of(text)
    classes, expected = _class_schema(ticker)
    roles = _role_assertions(soup)
    baselines: list[OwnershipBaseline] = []
    warnings: list[str] = []
    context_name = ""
    for table, rows in tables:
      for row_index, cells in enumerate(rows):
        first = cells[0]
        lower = first.lower()
        if any(token in lower for token in ("beneficial owner", "shares beneficially owned", "name of", "class ")):
            continue
        if (lower.endswith(":") or len(cells) == 1) and (not re.search(r"\d", first) or any(term in lower for term in ("stockholder", "directors", "executive officers", "founder voting"))):
            context_name = lower
            continue
        if len(cells) < expected or not first:
            continue
        is_group = bool(re.search(r"\ball (?:current )?(?:directors|executive officers|executive officers and directors)(?:.*\bgroup\b|\s*\(\d+\s+persons?\))|founder total|shares subject to|designated founders|founder voting trust", lower)) or "founder voting control" in context_name
        is_major = bool(re.search(r"5% stockholder|greater than 5%|vanguard|blackrock|farallon|renaissance|aqr|emergence", context_name + " " + lower, re.I))
        if is_group and "all " not in lower and "founder total" not in lower:
            is_major = False
        # The table's final columns are percentages.  Non-empty cells are
        # stable in SEC HTML, while blank presentation cells are discarded.
        values = _collapse_percent_tokens(cells[1:])
        name = _name_without_markers(first)
        if not name or not _name_key(name):
            continue
        class_values: dict[str, float | None] = {}
        class_percents: list[str] = []
        if ticker == "EXEL":
            shares = _number(values[0]) if values else None
            percent, display = _percent(values[1] if len(values) > 1 else "")
            class_values[classes[0]] = shares
            class_percents = [display or ""]
            voting, voting_display = None, None
        elif ticker == "ZM":
            for class_index, class_name in enumerate(classes):
                base = class_index * 2
                class_values[class_name] = _number(values[base]) if len(values) > base else None
                _, display = _percent(values[base + 1] if len(values) > base + 1 else "")
                class_percents.append(display or "")
            voting, voting_display = _percent(values[4] if len(values) > 4 else "")
        else:  # PLTR
            for class_index, class_name in enumerate(classes):
                base = class_index * 2
                class_values[class_name] = _number(values[base]) if len(values) > base else None
                _, display = _percent(values[base + 1] if len(values) > base + 1 else "")
                class_percents.append(display or "")
            voting, voting_display = _percent(values[6] if len(values) > 6 else "")
        numeric_shares = [value for value in class_values.values() if value is not None]
        total = sum(numeric_shares) if numeric_shares else None
        displays = [item for item in class_percents if item]
        beneficial_display = class_percents[0] if len(classes) == 1 else ", ".join(f"{classes[i]} {value}" for i, value in enumerate(class_percents) if value)
        beneficial_percent = _number(class_percents[0]) if len(classes) == 1 and class_percents else None
        role_values = sorted(set(roles.get(_name_key(name), [])) | set(_roles_for_person(soup, name)))
        if is_group:
            role = "group"
        elif is_major and not role_values:
            role = "major_beneficial_owner"
        elif role_values:
            role = ",".join(role_values)
        elif "director" in context_name or "executive" in context_name:
            role = "director_or_named_executive_officer"
        else:
            role = "major_beneficial_owner" if is_major else None
        matched = _match_person(name, existing_people)
        person_id = matched.person_id if matched else (None if is_group or is_major else f"proxy:{filing.issuer_cik}:{_name_key(name)}")
        baseline_id = f"{filing.issuer_cik}:{filing.accession_number}:{_name_key(name)}:{ticker}"
        row_context = " | ".join(cells)
        row_footnotes = sorted(set(re.findall(r"\(\d+\)", first)))
        footnote_texts = _extract_footnote_texts(section_context, row_footnotes)
        acquirable = _acquirable_within_60_days(text, name)
        notes = f"Table section: {context_name or 'beneficial ownership'}; raw row: {row_context}"
        if displays:
            notes += f"; class percentage display: {beneficial_display}"
        if marker:
            notes = f"{notes}; {section_context[:900]}"
        baselines.append(OwnershipBaseline(
            baseline_id=baseline_id,
            ticker=ticker,
            company_name="",
            person_name=name,
            issuer_cik=filing.issuer_cik,
            source_accession=filing.accession_number,
            source_url=filing.source_url,
            local_source_path=filing.local_source_path or "",
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            source_form=filing.form_type,
            filing_date=filing.filing_date,
            as_of_date=as_of,
            person_id=person_id,
            role=role,
            beneficial_shares=total,
            beneficial_percent=beneficial_percent,
            beneficial_percent_display=beneficial_display or None,
            shares_acquirable_within_60_days=acquirable,
            options_included=(acquirable is not None),
            security_class=(classes[0] if len([v for v in class_values.values() if v is not None]) == 1 else "multiple"),
            class_breakdown=class_values,
            voting_power_percent=voting,
            voting_power_display=voting_display,
            voting_power_notes="Voting power is reported by the proxy and is not additive to Section 16 holdings.",
            ownership_notes=notes,
            source_context=section_context[:2500],
            footnotes=footnote_texts,
            source_method="proxy_table",
            is_major_beneficial_owner=is_major,
            is_group_record=is_group,
            created_at=filing.filing_date or as_of or "",
        ))
    if not baselines:
        warnings.append(f"No person rows parsed from {filing.accession_number}")
    return ProxyBaselineResult(filing, baselines, warnings, as_of, section_context, roles)


def _match_person(name: str, people: Sequence[InsiderPerson]) -> InsiderPerson | None:
    key = _name_key(name)
    if not key:
        return None
    exact = [person for person in people if _name_key(person.full_name) == key or any(_name_key(alias) == key for alias in person.aliases)]
    if exact:
        sec_exact = [person for person in exact if person.person_id.startswith("sec:")]
        if len(sec_exact) == 1:
            return sec_exact[0]
        if len(exact) == 1 and exact[0].person_id.startswith("sec:"):
            return exact[0]
    candidates = []
    for person in people:
        pkey = set(_name_key(person.full_name))
        if set(key).issubset(pkey) or pkey.issubset(set(key)):
            candidates.append(person)
            continue
        # Proxy biographies frequently use a nickname or an initial (Dan vs
        # Daniel, H.R. vs Herbert Raymond).  A unique surname plus compatible
        # given-name prefixes is a safe identity match; ambiguous surnames are
        # deliberately left unresolved.
        common = set(key).intersection(pkey)
        surname_match = bool(common) and (len(key) == 1 or len(pkey) == 1 or any(token in common for token in set(key) if len(token) > 4))
        if key and pkey and surname_match:
            shared = next(iter(common))
            given = set(key) - {shared}
            existing_given = set(pkey) - {shared}
            if not given or all(any(a.startswith(b[:3]) or b.startswith(a[:3]) for b in existing_given) for a in given):
                candidates.append(person)
    sec_candidates = [person for person in candidates if person.person_id.startswith("sec:")]
    if len(sec_candidates) == 1:
        return sec_candidates[0]
    if len(candidates) == 1:
        return candidates[0]
    return exact[0] if len(exact) == 1 else None


def enrich_people_from_baselines(store: InsiderStore, baselines: Iterable[OwnershipBaseline]) -> dict[str, int]:
    """Attach proxy roles/provenance to matched Section 16 people and add proxy-only people."""
    existing = store.list_people()
    updates: list[InsiderPerson] = []
    for baseline in baselines:
        if baseline.is_group_record or baseline.is_major_beneficial_owner or not baseline.person_id:
            continue
        matched = next((p for p in existing if p.person_id == baseline.person_id), None)
        role_list = [item.strip() for item in (baseline.role or "").split(",") if item.strip()]
        flags = {"is_director": "director" in role_list or "director_or_named" in (baseline.role or ""), "is_officer": any(item in role_list for item in ("officer", "CEO", "CFO")), "is_ceo": "CEO" in role_list, "is_cfo": "CFO" in role_list, "is_founder": "founder" in role_list}
        if matched is None:
            matched = InsiderPerson(person_id=baseline.person_id, ticker=baseline.ticker, company_name=baseline.company_name or baseline.ticker, full_name=baseline.person_name, roles=role_list, primary_role=role_list[0] if role_list else baseline.role, source_urls=[baseline.source_url], source_filing_ids=[baseline.source_accession], source_form_types=[baseline.source_form], source_filing_dates=[baseline.filing_date] if baseline.filing_date else [], source_local_paths=[baseline.local_source_path], issuer_cik=baseline.issuer_cik, source_url=baseline.source_url, filing_id=baseline.source_accession, filing_date=baseline.filing_date, form_type=baseline.source_form, local_source_path=baseline.local_source_path, role_source_urls=[baseline.source_url], role_source_filing_ids=[baseline.source_accession], role_source_form_types=[baseline.source_form], **flags)
        else:
            canonical_roles = {"CEO", "CFO", "founder", "director", "officer", "director_or_named_executive_officer"}
            # The latest official proxy is the authoritative role assertion
            # for this baseline.  Keep non-proxy descriptive roles, but
            # replace stale proxy-derived canonical roles on reruns.
            matched.roles = sorted((set(matched.roles) - canonical_roles).union(role_list))
            matched.primary_role = matched.primary_role or (role_list[0] if role_list else baseline.role)
            for field, value in flags.items():
                setattr(matched, field, bool(value))
            matched.role_source_urls = sorted(set(matched.role_source_urls + [baseline.source_url]))
            matched.role_source_filing_ids = sorted(set(matched.role_source_filing_ids + [baseline.source_accession]))
            matched.role_source_form_types = sorted(set(matched.role_source_form_types + [baseline.source_form]))
        updates.append(matched)
    return store.upsert_people(updates) if updates else {"added": 0, "updated": 0, "unchanged": 0}


def reconcile_baseline(
    baseline: OwnershipBaseline,
    positions: Sequence[InsiderOwnershipPosition],
    transactions: Sequence[InsiderTransaction] = (),
) -> OwnershipReconciliation:
    """Reconcile only a directly comparable single-class baseline conservatively."""
    related = [item for item in positions if baseline.person_id and item.person_id == baseline.person_id]
    position_ids = [item.ownership_id for item in related]
    latest = max(related, key=lambda item: (item.as_of_date or "", item.filing_date or ""), default=None)
    warning = None
    method = "proxy_only"
    reconstructed = None
    as_of = baseline.as_of_date
    if baseline.is_group_record or baseline.is_major_beneficial_owner:
        warning = "Group or major-beneficial-owner row is not a Section 16 insider position."
    elif baseline.beneficial_shares is None:
        warning = "Proxy reports no numeric beneficial share count."
    elif baseline.security_class == "multiple":
        warning = "Multiple security classes/rights require class-level reconciliation."
    else:
        row_context = (baseline.ownership_notes or "").lower()
        if "; raw row:" in row_context:
            row_context = row_context.split("; raw row:", 1)[1].split("; class percentage", 1)[0]
        footnote_context = " ".join(baseline.footnotes).lower()
        if any(token in row_context + " " + footnote_context for token in ("trust", "spouse", "disclaim", "shared beneficial")):
            warning = "Indirect or shared beneficial ownership context is not safely additive."
        else:
            later = [item for item in transactions if baseline.person_id and item.person_id == baseline.person_id and (item.transaction_date or "") > (baseline.as_of_date or "") and not item.derivative_security and (item.ownership_form or "").upper() == "D" and (item.nature_of_indirect_ownership or "") == ""]
            if later:
                delta = sum(item.change_in_direct_holdings or 0.0 for item in later)
                reconstructed = baseline.beneficial_shares + delta
                as_of = max(item.transaction_date or "" for item in later)
                if latest is not None and latest.shares_beneficially_owned is not None and abs(latest.shares_beneficially_owned - reconstructed) > 0.01:
                    warning = "Reconstructed direct change does not equal the latest Section 16 reported position."
                else:
                    method = "proxy_plus_section16_reconciled"
            elif latest is not None and latest.shares_beneficially_owned is not None:
                warning = "No post-proxy compatible transactions were available to update the baseline."
            else:
                warning = "No comparable Section 16 position was found."
    return OwnershipReconciliation(
        reconciliation_id=f"{baseline.baseline_id}:reconciliation",
        ticker=baseline.ticker,
        baseline_id=baseline.baseline_id,
        person_id=baseline.person_id,
        current_ownership_method=method,
        proxy_beneficial_shares=baseline.beneficial_shares,
        section16_reported_shares=latest.shares_beneficially_owned if latest else None,
        reconstructed_current_shares=reconstructed,
        as_of_date=as_of,
        section16_position_ids=position_ids,
        warning=warning,
        source_filing_ids=[baseline.source_accession] + [item.filing_id for item in related],
        created_at=baseline.filing_date or baseline.as_of_date or "",
    )


class ProxyBaselineProvider:
    """Fetch, cache, parse, enrich, and persist latest official proxies."""

    def __init__(self, sec_provider: SECSourceProvider | None = None, store: InsiderStore | None = None):
        self.sec = sec_provider or SECSourceProvider()
        self.store = store or InsiderStore()

    def latest_proxy(self, ticker: str, *, force: bool = False) -> SECFiling:
        filings = self.sec.discover_filings(ticker, forms=("DEF 14A", "DEFA14A"), force=force)
        if not filings:
            raise LookupError(f"No DEF 14A proxy found for {ticker}")
        # DEFA14A supplements can be filed after the definitive proxy but do
        # not necessarily contain the beneficial-ownership table.  Prefer the
        # latest definitive proxy and use a supplement only when no DEF 14A is
        # available.
        definitive = [item for item in filings if item.form_type.upper() == "DEF 14A"]
        return max(definitive or filings, key=lambda item: (item.filing_date or "", item.accession_number))

    def discover_major_beneficial_owner_filings(self, ticker: str, *, force: bool = False) -> list[SECFiling]:
        """Return official Schedule 13D/13G filings for separate large-owner evidence."""
        return self.sec.discover_filings(ticker, forms=("SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"), force=force)

    def acquire(self, ticker: str, *, force: bool = False) -> ProxyBaselineResult:
        filing = self.latest_proxy(ticker, force=force)
        body, filing = self.sec.download_filing(filing, force=force)
        identity = self.sec.resolve_issuer(ticker)
        result = parse_proxy_html(body, filing, existing_people=self.store.list_people())
        for baseline in result.baselines:
            baseline.company_name = identity.company_name
        self.store.upsert_ownership_baselines(result.baselines)
        enrich_people_from_baselines(self.store, result.baselines)
        reconciliations = [reconcile_baseline(item, self.store.get_current_holdings(ticker), self.store.get_transactions(ticker)) for item in result.baselines if not item.is_group_record]
        self.store.upsert_ownership_reconciliations(reconciliations)
        return result

    def acquire_many(self, tickers: Iterable[str], *, force: bool = False) -> dict[str, ProxyBaselineResult]:
        return {ticker.upper(): self.acquire(ticker, force=force) for ticker in tickers}


__all__ = ["ProxyBaselineProvider", "ProxyBaselineResult", "parse_proxy_html", "enrich_people_from_baselines", "reconcile_baseline"]
