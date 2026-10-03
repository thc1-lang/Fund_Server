"""Official-proxy voting classes and founder-control disclosures."""

from __future__ import annotations

import re
from typing import Iterable

from .board_governance_models import VotingClass, VotingControlSnapshot
from .governance_sources import CachedGovernanceSource


def _number(value: str) -> float | None:
    value = value.lower().replace(",", "").strip()
    words = {"one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "ten": 10.0}
    if value in words:
        return words[value]
    try:
        return float(value)
    except ValueError:
        return None


def extract_voting_structure(ticker: str, proxy: CachedGovernanceSource | None) -> tuple[list[VotingClass], VotingControlSnapshot]:
    if proxy is None:
        return [], VotingControlSnapshot(voting_control_id=f"governance-voting-control:{ticker.upper()}", ticker=ticker.upper(), economic_ownership={"kind": "economic_ownership", "status": "UNKNOWN", "value": None}, voting_ownership={"kind": "voting_ownership", "status": "UNKNOWN", "value": None})
    text = proxy.text
    classes: list[VotingClass] = []
    class_matches = list(re.finditer(r"Each share of (?:our )?((?:Class|Series)\s+[A-Z][^,.;]{0,80}?(?:common|ordinary)?\s*stock?)\s+is entitled to\s+([\w.]+)\s+votes?\s+per share|Each share of (?:our )?((?:Class|Series)\s+[A-Z][^,.;]{0,80}?(?:common|ordinary)?\s*stock?)\s+is entitled to\s+([\w.]+)\s+vote", text, re.IGNORECASE))
    for match in class_matches:
        class_name = (match.group(1) or match.group(3) or "").strip()
        votes = _number(match.group(2) or match.group(4) or "")
        if not class_name:
            continue
        snippet = text[max(0, match.start() - 120):match.end() + 450]
        shares_match = re.search(r"([\d,]+)\s+shares?\s+of\s+" + re.escape(class_name), text[max(0, match.start() - 600):match.start() + 500], re.IGNORECASE)
        classes.append(VotingClass(
            voting_class_id=f"governance-voting-class:{ticker.upper()}:{re.sub(r'[^a-z0-9]+','-',class_name.lower()).strip('-')}", ticker=ticker.upper(), class_name=class_name,
            shares_outstanding=int(shares_match.group(1).replace(",", "")) if shares_match else None,
            votes_per_share=votes, conversion_rights="convertible" if "convert" in snippet.lower() else None,
            transfer_restrictions="restricted" if "transfer" in snippet.lower() and "restrict" in snippet.lower() else None,
            sunset_provisions="sunset" if "sunset" in snippet.lower() else None,
            holder_restrictions=None, source_url=proxy.source_url, source_accession=proxy.accession_number,
            source_form=proxy.form_type, source_date=proxy.filing_date, evidence_text=snippet[:1800],
        ))
    # Some multi-class proxies describe a special founder class in a separate
    # paragraph rather than using the "Each share ..." sentence.
    if re.search(r"Class F common stock", text, re.IGNORECASE) and not any("class f" in item.class_name.lower() for item in classes):
        f_match = re.search(r"Class F common stock[^.]{0,120}(?:has|is entitled to)\s+(\d+(?:\.\d+)?)\s+votes?", text, re.IGNORECASE)
        classes.append(VotingClass(
            voting_class_id=f"governance-voting-class:{ticker.upper()}:class-f-common-stock", ticker=ticker.upper(), class_name="Class F common stock",
            # A founder class is frequently governed by a formula, trust or
            # agreement.  Only populate a fixed vote count when the source
            # states it in the same explicit sentence; otherwise preserve the
            # structural fact with UNKNOWN rather than inventing a ratio.
            votes_per_share=_number(f_match.group(1)) if f_match else None,
            conversion_rights="convertible" if "class f common stock" in text.lower() and "convert" in text.lower() else None,
            structural_notes={
                "voting_formula": "UNKNOWN" if not f_match else "explicit fixed votes statement",
                "cap_or_sunset": "UNKNOWN" if "sunset" not in text.lower() else "explicit sunset language present",
            },
            source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
            evidence_text=(text[max(0, text.lower().find("class f common stock") - 120):text.lower().find("class f common stock") + 550]),
        ))
    if not classes:
        match = re.search(r"Each share of (?:our )?common stock is entitled to\s+([\w.]+)\s+votes?\s+per share|Each share of (?:our )?common stock is entitled to\s+([\w.]+)\s+vote", text, re.IGNORECASE)
        if match:
            classes.append(VotingClass(
                voting_class_id=f"governance-voting-class:{ticker.upper()}:common-stock", ticker=ticker.upper(), class_name="Common Stock",
                votes_per_share=_number(match.group(1) or match.group(2) or ""), source_url=proxy.source_url,
                source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
                evidence_text=match.group(0),
            ))
    unique_classes = {item.voting_class_id: item for item in classes}
    # Some proxies place conversion language in the ownership table rather
    # than beside the vote-per-share sentence.  Preserve that explicit right
    # without inferring any sunset or transfer rule.
    for item in unique_classes.values():
        if item.class_name.lower().startswith("class b") and re.search(r"Class B common stock is convertible[^.]{0,220}Class A common stock", text, re.IGNORECASE):
            item.conversion_rights = "convertible into Class A common stock"
    if not unique_classes and re.search(r"\bshares? of (?:our )?common stock\b", text, re.IGNORECASE):
        unique_classes["common-stock"] = VotingClass(
            voting_class_id=f"governance-voting-class:{ticker.upper()}:common-stock", ticker=ticker.upper(), class_name="Common Stock",
            source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
            evidence_text="Common stock is the only class expressly identified in the cached proxy; votes per share not stated in the extracted passage.",
        )
    structure = "unknown" if not unique_classes else "single_class" if len(unique_classes) == 1 else "dual_class" if len(unique_classes) == 2 else "multi_class"
    founder_window = ""
    founder_match = re.search(r"(?:Founder Voting Trust|Founder Voting Agreement|our Founders)", text, re.IGNORECASE)
    if founder_match:
        founder_window = text[max(0, founder_match.start() - 500):founder_match.start() + 5000]
    founder_names = re.search(r"(?:Stephen Cohen|Alexander Karp|Peter Thiel|Eric S\. Yuan)", founder_window or text, re.IGNORECASE)
    founder_control: dict[str, object] = {
        "disclosed": bool(founder_window and founder_names),
        "voting_percentage": None,
        "economic_percentage": None,
        "voting_control_basis": "UNKNOWN",
        "economic_ownership_status": "UNKNOWN",
        "as_of_date": proxy.filing_date,
    }
    if founder_window:
        percentage = re.search(r"(\d+(?:\.\d+)?)%[^.]{0,80}(?:voting|vote|control)", founder_window, re.IGNORECASE)
        if percentage:
            founder_control["voting_percentage"] = float(percentage.group(1))
            founder_control["voting_control_basis"] = "proxy disclosure of voting power under founder voting structure"
    agreement = []
    if founder_window:
        agreement.append({"structure": "voting trust/agreement", "parties": sorted(set(re.findall(r"Stephen Cohen|Alexander Karp|Peter Thiel|Wilmington Trust, National Association", founder_window, flags=re.IGNORECASE))), "evidence": founder_window[:2400]})
    snapshot = VotingControlSnapshot(
        voting_control_id=f"governance-voting-control:{ticker.upper()}", ticker=ticker.upper(), share_class_structure=structure,
        economic_ownership={"kind": "economic_ownership", "status": "UNKNOWN", "value": None}, voting_ownership={"kind": "voting_ownership", "status": "UNKNOWN", "value": None},
        founder_control=founder_control, voting_agreements=agreement,
        structural_notes={
            "class_f_voting_formula": "UNKNOWN" if any(item.class_name.lower() == "class f common stock" and item.votes_per_share is None for item in unique_classes.values()) else None,
            "class_f_cap_or_sunset": "UNKNOWN" if any(item.class_name.lower() == "class f common stock" for item in unique_classes.values()) else None,
            "economic_ownership_not_inferred": True,
        },
        control_as_of_date=proxy.filing_date, source_url=proxy.source_url,
        source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
        evidence_text=(founder_window or text[:2500])[:4000],
    )
    return sorted(unique_classes.values(), key=lambda item: item.voting_class_id), snapshot


__all__ = ["extract_voting_structure"]
