from __future__ import annotations

from collections import Counter
from typing import Any

from .confidence import evidence_confidence
from .factor_registry import load_mappings
from .models import FactorDefinition, FactorScore
from .sources import EvidenceSources
from .utils import make_factor


_EXCLUDED = ("is_automatic_sale", "is_10b5_1", "is_option_exercise", "is_equity_award", "is_tax_withholding", "is_gift")


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return sorted(rows, key=lambda row: (str(row.get("as_of_date", "")), str(row.get("created_at", ""))))[-1] if rows else None


def score_insider(ticker: str, as_of: str, profile: str, definitions: list[FactorDefinition], sources: EvidenceSources, config_root: str = "config/scoring") -> list[FactorScore]:
    results: list[FactorScore] = []
    mappings = load_mappings(config_root)
    alignment = _latest(sources.alignment(ticker))
    for definition in definitions:
        if definition.input_source == "alignment_snapshots":
            value = alignment.get("known_insider_percent") if alignment else None
            if value is None:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                           status="UNSCORED", coverage=0.0, confidence=0.0,
                                           raw_inputs={"known_insider_percent": None, "ownership_record": alignment},
                                           supporting_record_ids=[str(alignment.get("ticker"))] if alignment else [],
                                           coverage_reason="ECONOMIC_BENEFICIAL_PERCENT_NOT_DISCLOSED"))
            else:
                score = None
                for band in mappings.get("ownership_percent_bands", []):
                    if float(band["min"]) <= float(value) < float(band["max"]):
                        score = float(band["score"])
                        break
                if score is None:
                    results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                               status="UNKNOWN", coverage=1.0, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1),
                                               raw_inputs={"known_insider_percent": value}, supporting_record_ids=[str(alignment.get("ticker"))],
                                               coverage_reason="OWNERSHIP_PERCENT_OUTSIDE_CONFIGURED_BANDS"))
                    continue
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                                           status="SCORED", coverage=1.0, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1),
                                           raw_inputs={"known_insider_percent": value}, normalized_inputs={"economic_beneficial_ownership_percent": value},
                                           supporting_record_ids=[str(alignment.get("ticker"))], coverage_reason="KNOWN_BENEFICIAL_OWNERSHIP_PERCENT_CONFIGURED_BAND"))
        elif definition.input_source == "transactions":
            observed = sources.transactions(ticker)
            rows = [row for row in observed if row.get("is_open_market") and str(row.get("transaction_type", "")) in {"open_market_purchase", "open_market_sale"} and not any(row.get(flag) for flag in _EXCLUDED)]
            excluded = [row for row in observed if row not in rows]
            excluded_ids = [str(row.get("transaction_id")) for row in excluded if row.get("transaction_id")]
            excluded_counts = dict(Counter(str(row.get("transaction_type", "UNKNOWN")) for row in excluded))
            if not rows:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                           status="UNSCORED", coverage=0.0, confidence=0.0,
                                           raw_inputs={"eligible_transactions": 0, "observed_transactions": len(observed), "excluded_transaction_count": len(excluded), "excluded_transaction_ids": excluded_ids, "excluded_transaction_types": excluded_counts},
                                           coverage_reason="NO_SIGNAL_NO_DISCRETIONARY_OPEN_MARKET_ACTIVITY"))
            else:
                buys = sum(1 for row in rows if row.get("transaction_type") == "open_market_purchase")
                sells = len(rows) - buys
                score = 50.0 + 50.0 * ((buys - sells) / len(rows))
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                                           status="SCORED", coverage=1.0, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(rows), sample_size=len(rows)),
                                           raw_inputs={"eligible_transactions": len(rows), "observed_transactions": len(observed), "buyers": buys, "sellers": sells,
                                                       "percent_of_pre_transaction_holdings": [row.get("percent_of_pre_transaction_holdings") for row in rows], "excluded_transaction_count": len(excluded), "excluded_transaction_ids": excluded_ids, "excluded_transaction_types": excluded_counts},
                                           normalized_inputs={"direction": "purchase" if buys > sells else "sale" if sells > buys else "mixed"},
                                           supporting_record_ids=[str(row.get("transaction_id")) for row in rows], coverage_reason="DISCRETIONARY_OPEN_MARKET_TRANSACTIONS"))
        elif definition.input_source == "clusters":
            rows = [row for row in sources.clusters(ticker) if str(row.get("transaction_type", "")) in {"open_market_purchase", "open_market_sale"}]
            if not rows:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                           status="UNSCORED", coverage=0.0, confidence=0.0, raw_inputs={"eligible_clusters": 0},
                                           coverage_reason="NO_ELIGIBLE_DISCRETIONARY_CLUSTER"))
            else:
                buys = sum(1 for row in rows if row.get("transaction_type") == "open_market_purchase")
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=100.0 if buys else 0.0,
                                           status="SCORED", coverage=1.0, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(rows)),
                                           raw_inputs={"eligible_clusters": len(rows), "purchase_clusters": buys},
                                           supporting_record_ids=[str(row.get("cluster_id")) for row in rows], coverage_reason="ELIGIBLE_DISCRETIONARY_CLUSTER"))
    return results
