"""Namespace-tolerant parsing for SEC ownership XML (Forms 3, 4, and 5)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .models import (
    PARSER_VERSION,
    SECParsedFiling,
    SECFiling,
    InsiderOwnershipPosition,
    InsiderPerson,
    InsiderTransaction,
)
from .transaction_classification import classify_transaction, detect_10b5_1


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node, name: str):
    return [child for child in list(node) if _local(child.tag) == name]


def _first(node, name: str):
    if node is None:
        return None
    for child in node.iter():
        if _local(child.tag) == name:
            return child
    return None


def _value(node, name: str, default: str | None = None) -> str | None:
    child = _first(node, name)
    if child is None:
        return default
    value = _first(child, "value")
    text = value.text if value is not None else child.text
    return text.strip() if text and text.strip() else default


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9eE+\-.]", "", str(value).replace(",", ""))
    if not cleaned or cleaned in {".", "-", "+"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    return None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        return match.group(1) if match else value


def _plan_adoption_date(footnotes: list[str]) -> str | None:
    text = " ".join(footnotes)
    match = re.search(r"(?:adopted|established|entered into).*?\b(?:on|dated)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(match.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            return None


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def stable_person_id(issuer_cik: str, owner_cik: str | None, name: str) -> str:
    if owner_cik:
        return f"sec:{issuer_cik}:{owner_cik}"
    return "sec-name:" + hashlib.sha256(f"{issuer_cik}:{_name_key(name)}".encode()).hexdigest()[:24]


def _footnotes(root) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in root.iter():
        if _local(node.tag) != "footnote":
            continue
        identifier = node.attrib.get("id") or node.attrib.get("ID")
        text = " ".join("".join(node.itertext()).split())
        if identifier and text:
            result[identifier] = text
    return result


def _row_footnotes(row, footnote_map: dict[str, str]) -> list[str]:
    values: list[str] = []
    for node in row.iter():
        if _local(node.tag) != "footnoteId":
            continue
        identifier = node.attrib.get("id") or node.attrib.get("ID")
        if identifier and identifier in footnote_map:
            values.append(footnote_map[identifier])
    return list(dict.fromkeys(values))


def _issuer(root, filing: SECFiling) -> tuple[str, str, str]:
    issuer = _first(root, "issuer")
    name = _value(issuer, "issuerName") or ""
    cik = _value(issuer, "issuerCik") or filing.issuer_cik
    ticker = _value(issuer, "issuerTradingSymbol") or filing.ticker
    return name, ticker, cik.zfill(10)


def _owner(root, filing: SECFiling) -> tuple[str, str | None, dict[str, str | bool | None], list[str]]:
    owner = _first(root, "reportingOwner")
    owner_id = _first(owner, "reportingOwnerId")
    relationship = _first(owner, "reportingOwnerRelationship")
    name = _value(owner_id, "rptOwnerName") or "Unknown reporting owner"
    cik = _value(owner_id, "rptOwnerCik")
    data: dict[str, str | bool | None] = {
        "is_director": _bool(_value(relationship, "isDirector")),
        "is_officer": _bool(_value(relationship, "isOfficer")),
        "officer_title": _value(relationship, "officerTitle"),
        "is_ten_percent_owner": _bool(_value(relationship, "isTenPercentOwner")),
        "other_relationship": _value(relationship, "otherText"),
    }
    roles: list[str] = []
    if data["is_director"]:
        roles.append("director")
    if data["is_officer"]:
        roles.append("officer")
    if data["is_ten_percent_owner"]:
        roles.append("10%_owner")
    if data["other_relationship"]:
        roles.append(str(data["other_relationship"]))
    return name, cik, data, roles


def _make_person(filing: SECFiling, issuer_name: str, owner_name: str, owner_cik: str | None, data, roles: list[str]) -> InsiderPerson:
    title = str(data.get("officer_title") or "")
    title_lower = title.lower()
    primary = "CEO" if "chief executive" in title_lower or re.search(r"\bceo\b", title_lower) else "CFO" if "chief financial" in title_lower or re.search(r"\bcfo\b", title_lower) else title or (roles[0] if roles else None)
    return InsiderPerson(
        person_id=stable_person_id(filing.issuer_cik, owner_cik, owner_name),
        ticker=filing.ticker.upper(),
        company_name=issuer_name,
        full_name=owner_name,
        roles=roles,
        primary_role=primary,
        officer_title=data.get("officer_title"),
        other_relationship=data.get("other_relationship"),
        is_director=data.get("is_director"),
        is_officer=data.get("is_officer"),
        is_ceo=primary == "CEO" if primary else None,
        is_cfo=primary == "CFO" if primary else None,
        is_founder=any("founder" in role.lower() for role in roles) or None,
        is_ten_percent_owner=data.get("is_ten_percent_owner"),
        reporting_owner_cik=owner_cik,
        source_urls=[filing.source_url],
        source_filing_ids=[filing.accession_number],
        source_form_types=[filing.form_type],
        source_filing_dates=[filing.filing_date] if filing.filing_date else [],
        source_local_paths=[filing.local_source_path] if filing.local_source_path else [],
        issuer_cik=filing.issuer_cik,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        source_url=filing.source_url,
        filing_id=filing.accession_number,
        filing_date=filing.filing_date,
        form_type=filing.form_type,
        local_source_path=filing.local_source_path,
        source_authority=filing.source_authority,
    )


def _holding_rows(root, derivative: bool = False):
    wanted = "derivativeHolding" if derivative else "nonDerivativeHolding"
    return [node for node in root.iter() if _local(node.tag) == wanted]


def _ownership_fields(row, footnote_map: dict[str, str] | None = None):
    post = _first(row, "postTransactionAmounts")
    shares = _float(_value(post, "sharesOwnedFollowingTransaction"))
    ownership = _value(post, "directOrIndirectOwnership") or _value(row, "directOrIndirectOwnership")
    nature = _value(post, "natureOfOwnership") or _value(row, "natureOfOwnership")
    # Section 16 XML commonly stores only "See footnote" in the ownership
    # nature field. Resolve the linked source text so indirect ownership is
    # not silently reduced to an opaque placeholder.
    if nature and nature.strip().lower().startswith("see footnote") and footnote_map:
        notes = _row_footnotes(row, footnote_map)
        relevant = [
            note for note in notes
            if re.search(r"trust|spouse|joint|held|beneficial|pecuniary|disclaim", note, flags=re.IGNORECASE)
        ]
        if relevant:
            nature = f"{nature}: " + " ".join(dict.fromkeys(relevant))
    return shares, ownership, nature


def _ownership_position(
    row,
    filing: SECFiling,
    person: InsiderPerson,
    issuer_cik: str,
    derivative: bool,
    row_index: int,
    *,
    ownership_method: str = "reported",
    as_of_date: str | None = None,
    footnote_map: dict[str, str] | None = None,
) -> InsiderOwnershipPosition:
    title = _value(row, "securityTitle") or _value(row, "underlyingSecurity")
    shares, ownership, nature = _ownership_fields(row, footnote_map)
    direct_shares = shares if ownership == "D" else None
    indirect_shares = shares if ownership == "I" else None
    return InsiderOwnershipPosition(
        ownership_id=f"{person.person_id}:{filing.accession_number}:{title or 'unknown'}:{'derivative' if derivative else 'non_derivative'}:{ownership_method}:{row_index}",
        ticker=filing.ticker.upper(),
        person_id=person.person_id,
        as_of_date=as_of_date or _date(_value(row, "transactionDate") or filing.report_date or filing.filing_date),
        shares_owned_direct=direct_shares,
        shares_owned_indirect=indirect_shares,
        shares_beneficially_owned=shares,
        options_or_derivatives=shares if derivative else None,
        source_url=filing.source_url,
        filing_id=filing.accession_number,
        filing_date=filing.filing_date,
        issuer_cik=issuer_cik,
        reporting_owner_cik=person.reporting_owner_cik,
        form_type=filing.form_type,
        local_source_path=filing.local_source_path or "",
        ownership_method=ownership_method,
        direct_indirect=ownership,
        nature_of_indirect_ownership=nature,
        security_title=title,
        security_type="derivative" if derivative else "common_stock",
        source_row_index=row_index,
    )


def _transaction(
    row,
    filing: SECFiling,
    person: InsiderPerson,
    issuer_name: str,
    issuer_ticker: str,
    issuer_cik: str,
    footnote_map: dict[str, str],
    derivative: bool,
    row_index: int,
    root_10b5: bool | None,
) -> InsiderTransaction:
    title = _value(row, "securityTitle") or _value(row, "underlyingSecurity")
    date = _date(_value(row, "transactionDate"))
    deemed = _date(_value(row, "deemedExecutionDate"))
    coding = _first(row, "transactionCoding")
    code = _value(coding, "transactionCode")
    form_type = _value(coding, "transactionFormType") or filing.form_type
    acquired = _value(row, "transactionAcquiredDisposedCode")
    amounts = _first(row, "transactionAmounts")
    shares = _float(_value(amounts, "transactionShares"))
    price = _float(_value(amounts, "transactionPricePerShare"))
    total = _float(_value(amounts, "transactionTotalValue"))
    value = total if total is not None else shares * price if shares is not None and price is not None else None
    shares_after, ownership_form, nature = _ownership_fields(row, footnote_map)
    footnotes = _row_footnotes(row, footnote_map)
    explicit_10b5 = _bool(_value(coding, "aff10b5One"))
    if explicit_10b5 is None:
        explicit_10b5 = _bool(_value(row, "rule10b51"))
    if explicit_10b5 is None:
        explicit_10b5 = root_10b5
    classification = classify_transaction(code, acquired, security_title=title, footnotes=footnotes, is_10b5_1=explicit_10b5)
    underlying = _first(row, "underlyingSecurity")
    exercise_price = _float(_value(row, "exercisePrice"))
    expiry = _date(_value(row, "expirationDate"))
    underlying_shares = _float(_value(underlying, "underlyingSecurityShares")) if underlying is not None else None
    identity = "|".join((filing.accession_number, person.person_id, date or "", title or "", str(row_index), "D" if derivative else "N"))
    tx_id = "sec-tx:" + hashlib.sha256(identity.encode()).hexdigest()[:28]
    pre_candidate = shares_after - shares if acquired == "A" and shares_after is not None and shares is not None else shares_after + shares if acquired == "D" and shares_after is not None and shares is not None else None
    pre = pre_candidate if pre_candidate is None or pre_candidate >= 0 else None
    pre_method = "derived" if pre is not None else "unknown"
    pct_pre = shares / pre * 100 if pre and shares is not None else None
    pct_post = shares / shares_after * 100 if shares_after and shares is not None else None
    signed_change = shares if acquired == "A" else -shares if acquired == "D" and shares is not None else None
    return InsiderTransaction(
        transaction_id=tx_id,
        ticker=filing.ticker.upper(),
        person_id=person.person_id,
        transaction_date=date,
        filing_date=filing.filing_date,
        security_type="derivative" if derivative else "non_derivative",
        transaction_code=code,
        transaction_type=classification.transaction_type,
        shares=shares,
        price=price,
        transaction_value=value,
        acquired_or_disposed=acquired,
        shares_owned_after=shares_after,
        ownership_form=ownership_form,
        is_open_market=classification.is_open_market,
        is_option_exercise=classification.is_option_exercise,
        is_equity_award=classification.is_equity_award,
        is_tax_withholding=classification.is_tax_withholding,
        is_gift=classification.is_gift,
        is_automatic_sale=classification.is_automatic_sale,
        is_10b5_1=classification.is_10b5_1,
        footnotes=footnotes,
        source_url=filing.source_url,
        filing_id=filing.accession_number,
        filing_date_reported=filing.filing_date,
        issuer_cik=issuer_cik,
        reporting_owner_cik=person.reporting_owner_cik,
        form_type=filing.form_type,
        local_source_path=filing.local_source_path or "",
        issuer_name=issuer_name,
        issuer_ticker=issuer_ticker,
        security_title=title,
        deemed_execution_date=deemed,
        transaction_form_type=form_type,
        nature_of_indirect_ownership=nature,
        derivative_security=derivative,
        exercise_price=exercise_price,
        expiration_date=expiry,
        underlying_security=_value(underlying, "securityTitle") if underlying is not None else None,
        underlying_shares=underlying_shares,
        source_row_index=row_index,
        plan_adoption_date=_plan_adoption_date(footnotes),
        pre_transaction_shares=pre,
        pre_holdings_method=pre_method,
        percent_of_pre_transaction_holdings=pct_pre,
        percent_of_post_transaction_holdings=pct_post,
        change_in_direct_holdings=signed_change if ownership_form == "D" else None,
        change_in_economic_exposure=signed_change,
        classification_confidence=classification.confidence,
    )


def _reconcile_same_day_sequence(transactions: list[InsiderTransaction]) -> tuple[list[InsiderTransaction], list[str]]:
    """Keep derived pre-holdings only when same-day XML ordering is coherent."""
    groups: dict[tuple[str, str, str | None, str | None, str | None], list[InsiderTransaction]] = {}
    for item in transactions:
        key = (item.person_id, item.transaction_date or "", item.security_type, item.security_title, item.ownership_form)
        groups.setdefault(key, []).append(item)
    warnings: list[str] = []
    result = list(transactions)
    by_id = {item.transaction_id: index for index, item in enumerate(result)}
    for key, group in groups.items():
        if len(group) < 2:
            continue
        if any(item.source_row_index is None for item in group):
            ordered = group
            coherent = False
        else:
            ordered = sorted(group, key=lambda item: int(item.source_row_index))
            coherent = True
            for previous, current in zip(ordered, ordered[1:]):
                if previous.shares_owned_after is None or current.pre_transaction_shares is None or abs(previous.shares_owned_after - current.pre_transaction_shares) > 1e-9:
                    coherent = False
                    break
        if coherent:
            continue
        warnings.append(f"same-day transaction sequence unresolved for {key[0]} {key[2]} {key[3]}")
        for item in group:
            index = by_id[item.transaction_id]
            result[index] = item.__class__(**{**item.to_dict(), "pre_transaction_shares": None, "pre_holdings_method": "unknown", "percent_of_pre_transaction_holdings": None})
    return result, warnings


def parse_ownership_xml(xml: bytes | str, filing: SECFiling) -> SECParsedFiling:
    """Parse Forms 3/4/5 XML while keeping holdings separate from trades."""
    root = ET.fromstring(xml)
    issuer_name, issuer_ticker, issuer_cik = _issuer(root, filing)
    owner_name, owner_cik, owner_data, roles = _owner(root, filing)
    person = _make_person(filing, issuer_name, owner_name, owner_cik, owner_data, roles)
    footnote_map = _footnotes(root)
    positions: list[InsiderOwnershipPosition] = []
    holding_index = 0
    for derivative in (False, True):
        for row in _holding_rows(root, derivative):
            positions.append(_ownership_position(row, filing, person, issuer_cik, derivative, holding_index, footnote_map=footnote_map))
            holding_index += 1
    rows = [
        (False, row) for row in root.iter() if _local(row.tag) == "nonDerivativeTransaction"
    ] + [
        (True, row) for row in root.iter() if _local(row.tag) == "derivativeTransaction"
    ]
    root_10b5_node = next((child for child in list(root) if _local(child.tag) == "aff10b5One"), None)
    root_10b5 = _bool(root_10b5_node.text if root_10b5_node is not None else None)
    transactions = [
        _transaction(row, filing, person, issuer_name, issuer_ticker, issuer_cik, footnote_map, derivative, index, root_10b5)
        for index, (derivative, row) in enumerate(rows)
    ]
    # Form 3 is an initial ownership report. Holdings are positions, never
    # open-market transactions merely because the XML contains share totals.
    if filing.form_type.upper().strip().split("/", 1)[0] == "3":
        transactions = []
    else:
        for index, (derivative, row) in enumerate(rows):
            shares_after, _, _ = _ownership_fields(row, footnote_map)
            if shares_after is not None:
                positions.append(_ownership_position(
                    row,
                    filing,
                    person,
                    issuer_cik,
                    derivative,
                    index,
                    ownership_method="reported_post_transaction",
                    as_of_date=transactions[index].transaction_date,
                    footnote_map=footnote_map,
                ))
    transactions, sequence_warnings = _reconcile_same_day_sequence(transactions)
    return SECParsedFiling(
        filing=filing,
        people=[person],
        ownership_positions=positions,
        transactions=transactions,
        parser_version=PARSER_VERSION,
        warnings=sequence_warnings,
    )


__all__ = ["parse_ownership_xml", "stable_person_id"]
