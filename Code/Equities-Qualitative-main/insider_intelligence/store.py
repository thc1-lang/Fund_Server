"""Persistent JSONL store for people, ownership, transactions, clusters, and snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, TypeVar

from .models import (
    InsiderAlignmentSnapshot,
    InsiderCluster,
    InsiderOwnershipPosition,
    InsiderPerson,
    InsiderTransaction,
    OwnershipBaseline,
    OwnershipReconciliation,
    SECParsedFiling,
)


T = TypeVar("T")
_AUTHORITY_RANK = {"sec_structured": 100, "sec_official_filing": 90, "issuer_ir": 70, "third_party": 10}


class InsiderStore:
    def __init__(self, root: str | Path = "artifacts/insider_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_root = self.root / "indexes"
        self.index_root.mkdir(parents=True, exist_ok=True)
        self.paths = {
            "people": self.root / "people.jsonl",
            "ownership": self.root / "ownership.jsonl",
            "transactions": self.root / "transactions.jsonl",
            "alignment_snapshots": self.root / "alignment_snapshots.jsonl",
            "clusters": self.root / "clusters.jsonl",
            "ownership_baselines": self.root / "ownership_baselines.jsonl",
            "ownership_reconciliations": self.root / "ownership_reconciliations.jsonl",
        }

    def _read(self, kind: str, cls) -> dict[str, T]:
        values: dict[str, T] = {}
        path = self.paths[kind]
        if not path.exists():
            return values
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = cls.from_dict(json.loads(line))
            except (TypeError, ValueError, KeyError):
                continue
            identity = self._identity(item, kind)
            values[str(identity)] = item
        return values

    @staticmethod
    def _identity(item, kind: str) -> str:
        if kind == "people":
            return str(item.person_id)
        if kind == "ownership":
            return str(item.ownership_id)
        if kind == "transactions":
            return str(item.transaction_id)
        if kind == "clusters":
            return str(item.cluster_id)
        if kind == "alignment_snapshots":
            return f"{item.ticker.upper()}:{item.as_of_date}"
        if kind == "ownership_baselines":
            return str(item.baseline_id)
        if kind == "ownership_reconciliations":
            return str(item.reconciliation_id)
        return str(getattr(item, "ticker", ""))

    def _write(self, kind: str, values: Iterable[T]) -> None:
        path = self.paths[kind]
        fd, temporary = tempfile.mkstemp(prefix=f"{kind}-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        target = Path(temporary)
        try:
            ordered = sorted(values, key=lambda item: self._identity(item, kind))
            target.write_text("".join(json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False) + "\n" for item in ordered), encoding="utf-8")
            os.replace(target, path)
            self._write_index(kind, ordered)
        finally:
            if target.exists():
                target.unlink()

    def _write_index(self, kind: str, values: list[T]) -> None:
        grouped: dict[str, list[str]] = {}
        for item in values:
            identity = self._identity(item, kind)
            ticker = getattr(item, "ticker", None)
            if ticker:
                grouped.setdefault(str(ticker).upper(), []).append(identity)
        (self.index_root / f"{kind}.json").write_text(json.dumps({"version": 1, "count": len(values), "by_ticker": grouped}, indent=2, sort_keys=True), encoding="utf-8")

    def list_people(self) -> list[InsiderPerson]:
        return sorted(self._read("people", InsiderPerson).values(), key=lambda item: (item.ticker, item.full_name, item.person_id))

    def list_ownership(self) -> list[InsiderOwnershipPosition]:
        return sorted(self._read("ownership", InsiderOwnershipPosition).values(), key=lambda item: (item.ticker, item.as_of_date or "", item.ownership_id))

    def list_transactions(self) -> list[InsiderTransaction]:
        return sorted(self._read("transactions", InsiderTransaction).values(), key=lambda item: (item.ticker, item.transaction_date or "", item.transaction_id))

    def list_clusters(self) -> list[InsiderCluster]:
        return sorted(self._read("clusters", InsiderCluster).values(), key=lambda item: (item.ticker, item.start_date, item.cluster_id))

    def list_alignment_snapshots(self) -> list[InsiderAlignmentSnapshot]:
        return sorted(self._read("alignment_snapshots", InsiderAlignmentSnapshot).values(), key=lambda item: (item.ticker, item.as_of_date))

    def list_ownership_baselines(self) -> list[OwnershipBaseline]:
        return sorted(self._read("ownership_baselines", OwnershipBaseline).values(), key=lambda item: (item.ticker, item.as_of_date or "", item.baseline_id))

    def list_ownership_reconciliations(self) -> list[OwnershipReconciliation]:
        return sorted(self._read("ownership_reconciliations", OwnershipReconciliation).values(), key=lambda item: (item.ticker, item.as_of_date or "", item.reconciliation_id))

    def get_ownership_baselines(self, ticker: str | None = None, person_id: str | None = None) -> list[OwnershipBaseline]:
        return [item for item in self.list_ownership_baselines() if (ticker is None or item.ticker.upper() == ticker.upper()) and (person_id is None or item.person_id == person_id)]

    def get_latest_ownership_baselines(self, ticker: str) -> list[OwnershipBaseline]:
        values = self.get_ownership_baselines(ticker)
        if not values:
            return []
        latest_key = max((item.as_of_date or "", item.filing_date or "", item.source_accession) for item in values)
        return [item for item in values if (item.as_of_date or "", item.filing_date or "", item.source_accession) == latest_key]

    def get_transactions(self, ticker: str | None = None, person_id: str | None = None) -> list[InsiderTransaction]:
        return [item for item in self.list_transactions() if (ticker is None or item.ticker.upper() == ticker.upper()) and (person_id is None or item.person_id == person_id)]

    def get_purchases(self, ticker: str | None = None) -> list[InsiderTransaction]:
        return [item for item in self.get_transactions(ticker) if item.transaction_type == "open_market_purchase"]

    def get_sales(self, ticker: str | None = None) -> list[InsiderTransaction]:
        return [item for item in self.get_transactions(ticker) if item.transaction_type in {"open_market_sale", "automatic_sale", "planned_sale"}]

    def get_ownership(self, ticker: str | None = None, person_id: str | None = None) -> list[InsiderOwnershipPosition]:
        return [item for item in self.list_ownership() if (ticker is None or item.ticker.upper() == ticker.upper()) and (person_id is None or item.person_id == person_id)]

    def get_current_holdings(self, ticker: str | None = None, filing_ids: set[str] | None = None) -> list[InsiderOwnershipPosition]:
        values = [item for item in self.get_ownership(ticker) if filing_ids is None or item.filing_id in filing_ids]
        latest: dict[tuple[str, str | None, str | None], InsiderOwnershipPosition] = {}
        for item in values:
            key = (item.person_id, item.security_title, item.direct_indirect)
            item_order = (item.as_of_date or "", item.filing_date or "", item.filing_id, item.source_row_index if item.source_row_index is not None else -1, item.ownership_id)
            current_order = latest.get(key)
            current_key = (current_order.as_of_date or "", current_order.filing_date or "", current_order.filing_id, current_order.source_row_index if current_order.source_row_index is not None else -1, current_order.ownership_id) if current_order is not None else None
            if current_key is None or item_order > current_key:
                latest[key] = item
        return list(latest.values())

    @staticmethod
    def provenance_errors(item) -> list[str]:
        errors: list[str] = []
        for field in ("source_url", "filing_id", "filing_date"):
            if not getattr(item, field, None):
                errors.append(f"{type(item).__name__}.{field} is required")
        return errors

    @staticmethod
    def _merge_person(old: InsiderPerson, new: InsiderPerson) -> InsiderPerson:
        old.roles = sorted(set(old.roles).union(new.roles))
        old.aliases = sorted(set(old.aliases).union(new.aliases).union({new.full_name} if new.full_name != old.full_name else set()))
        old.source_urls = sorted(set(old.source_urls).union(new.source_urls))
        old.source_filing_ids = sorted(set(old.source_filing_ids).union(new.source_filing_ids))
        old.source_form_types = sorted(set(old.source_form_types).union(new.source_form_types))
        old.source_filing_dates = sorted(set(old.source_filing_dates).union(new.source_filing_dates))
        old.source_local_paths = sorted(set(old.source_local_paths).union(new.source_local_paths))
        old.role_source_urls = sorted(set(old.role_source_urls).union(new.role_source_urls))
        old.role_source_filing_ids = sorted(set(old.role_source_filing_ids).union(new.role_source_filing_ids))
        old.role_source_form_types = sorted(set(old.role_source_form_types).union(new.role_source_form_types))
        for field in ("company_name", "primary_role", "reporting_owner_cik", "issuer_cik", "source_url", "filing_id", "filing_date", "form_type", "local_source_path"):
            if getattr(new, field) and not getattr(old, field):
                setattr(old, field, getattr(new, field))
        for field in ("is_director", "is_officer", "is_ceo", "is_cfo", "is_founder", "is_ten_percent_owner"):
            if getattr(new, field) is True:
                setattr(old, field, True)
            elif getattr(old, field) is None and getattr(new, field) is not None:
                setattr(old, field, getattr(new, field))
        old.updated_at = new.updated_at or old.updated_at
        return old

    def upsert_people(self, people: Iterable[InsiderPerson]) -> dict[str, int]:
        current = self._read("people", InsiderPerson)
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for person in people:
            previous = current.get(person.person_id)
            if previous is None:
                current[person.person_id] = person
                counts["added"] += 1
                changed = True
            else:
                previous_value = previous.to_dict()
                merged = self._merge_person(previous, person)
                if merged.to_dict() == previous_value:
                    counts["unchanged"] += 1
                else:
                    counts["updated"] += 1
                    changed = True
                current[person.person_id] = merged
        if changed or not self.paths["people"].exists():
            self._write("people", current.values())
        return counts

    def _upsert_records(self, kind: str, records: Iterable[T], identity_name: str, cls) -> dict[str, int]:
        current = self._read(kind, cls)
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for record in records:
            errors = self.provenance_errors(record) if kind in {"ownership", "transactions"} else []
            if errors:
                raise ValueError("; ".join(errors))
            identity = str(getattr(record, identity_name))
            previous = current.get(identity)
            if previous is not None and _AUTHORITY_RANK.get(getattr(previous, "source_authority", "third_party"), 0) > _AUTHORITY_RANK.get(getattr(record, "source_authority", "third_party"), 0):
                counts["unchanged"] += 1
                continue
            if previous is not None and previous.to_dict() == record.to_dict():
                counts["unchanged"] += 1
                continue
            current[identity] = record
            counts["updated" if previous is not None else "added"] += 1
            changed = True
        if changed or not self.paths[kind].exists():
            self._write(kind, current.values())
        return counts

    def upsert_ownership(self, positions: Iterable[InsiderOwnershipPosition]) -> dict[str, int]:
        return self._upsert_records("ownership", positions, "ownership_id", InsiderOwnershipPosition)

    def upsert_transactions(self, transactions: Iterable[InsiderTransaction]) -> dict[str, int]:
        return self._upsert_records("transactions", transactions, "transaction_id", InsiderTransaction)

    def upsert_clusters(self, clusters: Iterable[InsiderCluster]) -> dict[str, int]:
        return self._upsert_records("clusters", clusters, "cluster_id", InsiderCluster)

    def upsert_ownership_baselines(self, baselines: Iterable[OwnershipBaseline]) -> dict[str, int]:
        current = self._read("ownership_baselines", OwnershipBaseline)
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for record in baselines:
            required = (record.issuer_cik, record.source_accession, record.source_url, record.local_source_path)
            if not all(required):
                raise ValueError("OwnershipBaseline requires issuer CIK, accession, source URL, and local source path")
            identity = record.baseline_id
            previous = current.get(identity)
            if previous is not None and previous.to_dict() == record.to_dict():
                counts["unchanged"] += 1
                continue
            current[identity] = record
            counts["updated" if previous is not None else "added"] += 1
            changed = True
        if changed or not self.paths["ownership_baselines"].exists():
            self._write("ownership_baselines", current.values())
        return counts

    def upsert_ownership_reconciliations(self, records: Iterable[OwnershipReconciliation]) -> dict[str, int]:
        return self._upsert_records("ownership_reconciliations", records, "reconciliation_id", OwnershipReconciliation)

    def upsert_alignment_snapshots(self, snapshots: Iterable[InsiderAlignmentSnapshot]) -> dict[str, int]:
        records = list(snapshots)
        current = self._read("alignment_snapshots", InsiderAlignmentSnapshot)
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for record in records:
            identity = f"{record.ticker.upper()}:{record.as_of_date}"
            previous = current.get(identity)
            if previous is not None and previous.to_dict() == record.to_dict():
                counts["unchanged"] += 1
                continue
            current[identity] = record
            counts["updated" if previous is not None else "added"] += 1
            changed = True
        if changed or not self.paths["alignment_snapshots"].exists():
            self._write("alignment_snapshots", current.values())
        return counts

    def upsert_parsed(self, parsed: SECParsedFiling) -> dict[str, dict[str, int]]:
        return {
            "people": self.upsert_people(parsed.people),
            "ownership": self.upsert_ownership(parsed.ownership_positions),
            "transactions": self.upsert_transactions(parsed.transactions),
        }


__all__ = ["InsiderStore"]
