"""CLI and orchestration for factual person/career intelligence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .career_history import ManagementParseResult, parse_proxy_people
from .role_changes import parse_role_changes
from .models import ManagementRun
from .sec_sources import SECManagementSourceProvider
from insider_intelligence.sec_ingestion import SECSourceProvider
from insider_intelligence import InsiderStore
from .store import ManagementStore
from .identity_resolution import ManagementIdentityResolver
from .insider_links import link_management_to_insiders
from .models import ManagementPerson, PersonCompanyRelationship, RoleAssertion, BoardMembership
from .people import person_key, normalize_name
from .roles import classify_role, responsibilities_from_title


def _apply_role_changes(result: ManagementParseResult, changes, filing, company_name: str, *, as_of_date: str | None = None) -> None:
    """Make later Item 5.02 disclosures control current status."""
    by_id = {person.person_id: person for person in result.people}
    by_name = {normalize_name(person.full_name): person for person in result.people}
    for change in sorted(changes, key=lambda item: (item.effective_date or "", item.event_id)):
        person = by_id.get(change.person_id) if change.person_id else None
        if person is None and change.person_name:
            person = by_name.get(normalize_name(change.person_name))
        if person is None and change.event_type == "appointment" and change.person_name:
            person = ManagementPerson(
                person_id=person_key(filing.ticker, change.person_name),
                ticker=filing.ticker.upper(),
                company_name=company_name,
                full_name=change.person_name,
                normalized_name=normalize_name(change.person_name),
                issuer_cik=filing.issuer_cik,
                current_roles=[change.role or "Director"],
                role_categories=sorted(set(classify_role(change.role or "Director") or ["director"])),
                role_start_dates={change.role or "Director": change.effective_date} if change.effective_date else {},
                current_since=change.effective_date,
                source_ids=[change.source_id],
                source_urls=[change.source_url],
                source_local_paths=[change.local_source_path] if change.local_source_path else [],
                source_accession_numbers=[change.accession_number] if change.accession_number else [],
                source_form_types=[change.form_type],
                source_url=change.source_url,
                local_source_path=change.local_source_path,
                accession_number=change.accession_number,
                form_type=change.form_type,
                created_at=change.filing_date or "",
                updated_at=change.filing_date or "",
            )
            result.people.append(person)
            by_id[person.person_id] = person
            by_name[person.normalized_name] = person
            relationship_id = f"relationship:{person.person_id}:{filing.issuer_cik}"
            person.issuer_relationship_ids = [relationship_id]
            result.relationships.append(PersonCompanyRelationship(
                person_company_id=relationship_id,
                person_id=person.person_id,
                issuer_cik=filing.issuer_cik,
                ticker=filing.ticker.upper(),
                company_name=company_name,
                current_roles=list(person.current_roles),
                role_categories=list(person.role_categories),
                status="current",
                start_date=change.effective_date,
                date_precision="day" if change.effective_date else None,
                source_ids=[change.source_id],
                source_urls=[change.source_url],
                source_local_paths=[change.local_source_path] if change.local_source_path else [],
                source_accession_numbers=[change.accession_number] if change.accession_number else [],
                source_form_types=[change.form_type],
                source_url=change.source_url,
                local_source_path=change.local_source_path,
                accession_number=change.accession_number,
                form_type=change.form_type,
                evidence_text=change.evidence_text,
            ))
            role = change.role or "Director"
            category = "director" if "director" in role.lower() or role.lower() == "chair" else (classify_role(role) or ["officer"])[0]
            result.roles.append(RoleAssertion(
                assertion_id=f"role:8k:{change.event_id}",
                person_id=person.person_id,
                ticker=filing.ticker.upper(),
                issuer_cik=filing.issuer_cik,
                role=role,
                role_category=category,
                is_current=True,
                start_date=change.effective_date,
                date_precision="day" if change.effective_date else None,
                title_as_reported=role,
                responsibilities=responsibilities_from_title(role),
                evidence_text=change.evidence_text,
                source_ids=[change.source_id],
                source_urls=[change.source_url],
                source_local_paths=[change.local_source_path] if change.local_source_path else [],
                source_accession_numbers=[change.accession_number] if change.accession_number else [],
                source_form_types=[change.form_type],
                source_url=change.source_url,
                local_source_path=change.local_source_path,
                accession_number=change.accession_number,
                form_type=change.form_type,
            ))
            if category == "director":
                result.boards.append(BoardMembership(
                    board_id=f"board:{person.person_id}:{filing.ticker.upper()}",
                    person_id=person.person_id,
                    ticker=filing.ticker.upper(),
                    board_company=company_name,
                    issuer_cik=filing.issuer_cik,
                    person_company_id=relationship_id,
                    board_ticker=filing.ticker.upper(),
                    board_cik=filing.issuer_cik,
                    board_classification="PUBLIC_COMPANY_CONFIRMED",
                    board_role="director",
                    start_date=change.effective_date,
                    date_precision="day" if change.effective_date else None,
                    is_current=True,
                    evidence_text=change.evidence_text,
                    source_ids=[change.source_id],
                    source_urls=[change.source_url],
                    source_local_paths=[change.local_source_path] if change.local_source_path else [],
                    source_accession_numbers=[change.accession_number] if change.accession_number else [],
                    source_form_types=[change.form_type],
                    source_url=change.source_url,
                    local_source_path=change.local_source_path,
                    accession_number=change.accession_number,
                    form_type=change.form_type,
                ))
        if person is None:
            continue
        change.person_id = person.person_id
        if change.event_type == "departure":
            # A future-effective resignation is a scheduled change, not yet a
            # former role.  Keep the person current until the stated date.
            effective_now = not change.effective_date or change.effective_date <= (as_of_date or datetime.now(timezone.utc).date().isoformat())
            if not effective_now:
                continue
            person.current_roles = [role for role in person.current_roles if not (change.role and change.role.lower() in role.lower()) and not (change.role == "director" and "director" in role.lower())]
            for assertion in result.roles:
                if assertion.person_id == person.person_id and assertion.is_current and (not change.role or change.role.lower() in assertion.role.lower() or (change.role == "director" and "director" in assertion.role.lower())):
                    assertion.is_current = False
                    assertion.end_date = change.effective_date
            for board in result.boards:
                if board.person_id == person.person_id and board.issuer_cik == filing.issuer_cik and board.is_current and (not change.role or change.role.lower() in board.board_role.lower() or change.role == "director"):
                    board.is_current = False
                    board.end_date = change.effective_date
            for relationship in result.relationships:
                if relationship.person_id == person.person_id:
                    relationship.status = "historical"
                    relationship.end_date = change.effective_date
                    relationship.current_roles = list(person.current_roles)


class ManagementIntelligenceProvider:
    def __init__(self, *, store: ManagementStore | None = None, sec: SECManagementSourceProvider | None = None, insider_store: InsiderStore | None = None):
        self.store = store or ManagementStore()
        self.sec = sec or SECManagementSourceProvider()
        self.insider_store = insider_store or InsiderStore()
        self.identity = ManagementIdentityResolver()
        self.last_counts: dict[str, dict[str, int]] = {}
        self.last_warnings: list[str] = []

    def acquire(self, ticker: str, *, force: bool = False, as_of_date: str | None = None) -> ManagementParseResult:
        ticker = ticker.strip().upper()
        if not ticker:
            raise ValueError("ticker is required")
        identity = self.sec.issuer(ticker)
        filing, body = self.sec.latest_proxy(ticker, force=force, as_of_date=as_of_date)
        result = parse_proxy_people(body, filing, identity.company_name)
        existing = self.store.list_people()
        remap: dict[str, str] = {}
        resolved_people = []
        for person in result.people:
            outcome = self.identity.reconcile_incoming(person, existing + resolved_people)
            remap[person.person_id] = outcome.person.person_id
            resolved_people.append(outcome.person)
        if remap:
            for person in result.people:
                person.person_id = remap.get(person.person_id, person.person_id)
            for item in [*result.roles, *result.career, *result.boards, *result.relationships, *result.education]:
                old = getattr(item, "person_id", None)
                if old in remap:
                    item.person_id = remap[old]
            for item in result.relationships:
                item.person_company_id = f"relationship:{item.person_id}:{filing.issuer_cik}"
            for person in result.people:
                person.issuer_relationship_ids = [f"relationship:{person.person_id}:{filing.issuer_cik}"]
            for item in result.boards:
                item.person_company_id = f"relationship:{item.person_id}:{filing.issuer_cik}"
        links = link_management_to_insiders(result.people, [p for p in self.insider_store.list_people() if p.ticker.upper() == ticker], issuer_cik=filing.issuer_cik, source_ids=[result.source.source_id] if result.source else [])
        # Newer Item 5.02 filings can supersede proxy current-status claims;
        # preserve the proxy and record the newer event separately.
        for change_filing in self.sec.role_change_filings(ticker, after=filing.filing_date, as_of_date=as_of_date):
            try:
                body, cached_change = self.sec.download_filing(change_filing, force=force)
                self.store.sources.upsert_many([self.sec.source_for_filing(cached_change)])
                result.role_changes.extend(parse_role_changes(body, cached_change, result.people))
            except Exception as exc:
                result.warnings.append(f"8-K {change_filing.accession_number}: {exc}")
        result.role_changes = list({item.event_id: item for item in result.role_changes}.values())
        _apply_role_changes(result, result.role_changes, filing, identity.company_name, as_of_date=as_of_date)
        result.roles = list({item.assertion_id: item for item in result.roles}.values())
        result.boards = list({item.board_id: item for item in result.boards}.values())
        result.relationships = list({item.person_company_id: item for item in result.relationships}.values())
        # Role-change events may have been associated with an existing proxy
        # person or with a newly appointed person discovered in the 8-K.
        links = link_management_to_insiders(result.people, [p for p in self.insider_store.list_people() if p.ticker.upper() == ticker], issuer_cik=filing.issuer_cik, source_ids=[result.source.source_id] if result.source else [])
        if result.source is not None:
            self.store.sources.upsert_many([result.source])
        self.last_counts = {
            "people": self.store.upsert_people(result.people),
            "roles": self.store.upsert_roles(result.roles),
            "career": self.store.upsert_career(result.career),
            "boards": self.store.upsert_boards(result.boards),
            "relationships": self.store.upsert_relationships(result.relationships),
            "education": self.store.upsert_education(result.education),
            "insider_links": self.store.upsert_insider_links(links),
            "role_changes": self.store.upsert_role_changes(result.role_changes),
        }
        return result

    def acquire_many(self, tickers: Iterable[str], *, force: bool = False, as_of_date: str | None = None) -> dict[str, ManagementParseResult]:
        results: dict[str, ManagementParseResult] = {}
        warnings: list[str] = []
        for value in tickers:
            ticker = value.strip().upper()
            if not ticker:
                continue
            try:
                results[ticker] = self.acquire(ticker, force=force, as_of_date=as_of_date)
            except Exception as exc:  # per issuer isolation keeps a batch useful
                warnings.append(f"{ticker}: {exc}")
        run = ManagementRun(
            run_id=f"run:{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            tickers=sorted(results),
            sources=sorted({source_id for result in results.values() for source_id in ([result.source.source_id] if result.source else [])}),
            people_count=sum(len(result.people) for result in results.values()),
            roles_count=sum(len(result.roles) for result in results.values()),
            career_count=sum(len(result.career) for result in results.values()),
            boards_count=sum(len(result.boards) for result in results.values()),
            warnings=warnings + [warning for result in results.values() for warning in result.warnings],
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.store.record_run(run)
        self.last_warnings = list(run.warnings)
        return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire official SEC-backed management and board facts")
    parser.add_argument("ticker", nargs="+", help="issuer ticker(s)")
    parser.add_argument("--store-root", default="artifacts/management_intelligence")
    parser.add_argument("--sec-cache-root", default="artifacts/insider_intelligence/sec")
    parser.add_argument("--normalized-root", default=None, help="normalized SEC document store used as the authoritative source handoff")
    parser.add_argument("--as-of-date", default=None, help="exclude filings after this historical cutoff")
    parser.add_argument("--force", action="store_true", help="refresh official filings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = ManagementIntelligenceProvider(
        store=ManagementStore(args.store_root),
        sec=SECManagementSourceProvider(SECSourceProvider(cache_root=args.sec_cache_root), normalized_root=args.normalized_root),
    )
    results = provider.acquire_many(args.ticker, force=args.force, as_of_date=args.as_of_date)
    for ticker, result in sorted(results.items()):
        print(f"{ticker}: people={len(result.people)} roles={len(result.roles)} career={len(result.career)} boards={len(result.boards)}")
        for warning in result.warnings:
            print(f"WARNING {ticker}: {warning}")
    # Per-issuer isolation keeps a batch useful, but swallowing those errors
    # made pipeline manifests say merely "without records".  Surface the
    # precise affected ticker and cause for the orchestrator and operator.
    for warning in provider.last_warnings:
        if warning not in [item for result in results.values() for item in result.warnings]:
            print(f"WARNING {warning}")
    return 0 if results else 1


__all__ = ["ManagementIntelligenceProvider", "build_parser", "main"]
