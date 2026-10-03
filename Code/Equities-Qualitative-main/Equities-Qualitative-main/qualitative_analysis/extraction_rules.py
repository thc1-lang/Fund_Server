"""Small, auditable deterministic rules used by the initial provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .extraction_models import ClaimCandidate, CERTAINTY_VALUES, DIMENSIONS, DIRECTION_VALUES


@dataclass(frozen=True)
class ExtractionRule:
    dimension: str
    topic: str
    patterns: tuple[str, ...]


RULES: tuple[ExtractionRule, ...] = (
    ExtractionRule("guidance", "outlook", (r"\bguidance\b", r"\boutlook\b", r"\bforecast\b", r"\bguide(?:d|s|line)?\b", r"\bexpect(?:ed|s|ing)?\b")),
    ExtractionRule("demand", "demand", (r"\bdemand\b", r"\binterest\b", r"\bbookings?\b", r"\bwin rate\b")),
    ExtractionRule("revenue", "revenue", (r"\brevenue\b", r"\bnet sales\b", r"\bsales (?:grew|increased|were|of|to)\b", r"\bARR\b", r"\bannual recurring revenue\b")),
    ExtractionRule("pricing", "pricing", (r"\bpric(?:e|ing|ed)\b", r"\bprice increase\b", r"\bupsell\b")),
    ExtractionRule("volume", "volume", (r"\bvolume\b", r"\bunits?\b", r"\bshipments?\b")),
    ExtractionRule("margins", "margin", (r"\b(?:gross|operating|EBITDA|adjusted) margin\b", r"\bmargin(?:s)?\b")),
    ExtractionRule("costs", "cost", (r"\bcost(?:s)?\b", r"\bexpenses?\b", r"\bspend(?:ing)?\b")),
    ExtractionRule("operating_expenses", "operating expenses", (r"\boperating expenses?\b", r"\bOpEx\b")),
    ExtractionRule("capex", "capital expenditure", (r"\bcapex\b", r"\bcapital expenditures?\b")),
    ExtractionRule("cash_flow", "cash flow", (r"\bcash flow\b", r"\bfree cash flow\b")),
    ExtractionRule("backlog", "backlog", (r"\bbacklog\b", r"\bbook(?:ed|ings?)\b")),
    ExtractionRule("customer_growth", "customer growth", (r"\bcustomer(?:s)?\b.*\b(?:grew|growth|increase|added|expanded)\b", r"\bnew customers?\b")),
    ExtractionRule("customer_retention", "customer retention", (r"\bretention\b", r"\bchurn\b", r"\brenewal\b")),
    ExtractionRule("product", "product", (r"\bproduct(?:s)?\b", r"\bplatform\b", r"\bsolution\b", r"\blaunch(?:ed|ing)?\b", r"\bfeature(?:s)?\b")),
    ExtractionRule("geography", "geography", (r"\b(?:international|global|domestic|Europe|Asia|Americas)\b", r"\bgeograph(?:y|ic)\b")),
    ExtractionRule("competition", "competition", (r"\bcompet(?:e|es|ed|ing|ition)\b", r"\bcompetitive\b")),
    ExtractionRule("market_share", "market share", (r"\bmarket share\b", r"\bshare of the market\b")),
    ExtractionRule("capacity", "capacity", (r"\bcapacity\b", r"\bmanufacturing footprint\b")),
    ExtractionRule("supply_chain", "supply chain", (r"\bsupply chain\b", r"\bsupplier(?:s)?\b", r"\bshortage\b")),
    ExtractionRule("inventory", "inventory", (r"\binventor(?:y|ies)\b", r"\bstock levels?\b")),
    ExtractionRule("management", "management", (r"\bCEO\b", r"\bCFO\b", r"\bmanagement\b", r"\bleadership\b")),
    ExtractionRule("capital_allocation", "capital allocation", (r"\bshare repurchas(?:e|es|ed|ing)\b", r"\bbuyback\b", r"\bdividend\b", r"\bacquisition\b", r"\bdebt repayment\b")),
    ExtractionRule("balance_sheet", "balance sheet", (r"\bbalance sheet\b", r"\bliquidity\b", r"\bcash balance\b", r"\bdebt\b")),
    ExtractionRule("regulation", "regulation", (r"\bregulat(?:ion|ory|or)\b", r"\bapproval\b", r"\bFDA\b")),
    ExtractionRule("risks", "risk", (r"(?<!de-)\brisk(?:s|y)?\b", r"\bchallenge(?:s)?\b", r"\buncertain(?:ty|ties)?\b", r"\bheadwind(?:s)?\b")),
    ExtractionRule("catalysts", "catalyst", (r"\bcatalyst(?:s)?\b", r"\bopportunit(?:y|ies)\b", r"\btailwind(?:s)?\b")),
    ExtractionRule("strategy", "strategy", (r"\bstrateg(?:y|ic)\b", r"\bpriorit(?:y|ies|ize)\b", r"\bfocus(?:ed|ing)?\b")),
)

_PRIMARY_ORDER = (
    "guidance", "revenue", "demand", "margins", "capex", "capital_allocation", "costs", "operating_expenses",
    "cash_flow", "pricing", "volume", "backlog", "bookings",
    "customer_growth", "customer_retention", "product", "geography", "competition",
    "market_share", "capacity", "supply_chain", "inventory",
    "balance_sheet", "regulation", "risks", "catalysts", "strategy", "management",
)
_NOISE_PATTERNS = (
    r"\bforward[- ]looking statements?\b", r"\bactual results? may differ\b",
    r"\bsubject to (?:certain )?risks? and uncertainties\b", r"\bconsult the risk factors\b",
    r"\bdiscuss(?:ed|ed) in (?:our|the) (?:annual|quarterly) report\b",
    r"\bform 10-?[kq]\b", r"\bsafe harbor\b", r"\bfor additional detail\b",
    r"\b(?:i'm|we are) joined (?:today )?by\b", r"\bthank you for (?:the )?(?:question|joining|your time)\b",
    r"\b(?:good morning|good afternoon|good evening|welcome to)\b", r"\bplease go ahead\b",
    r"\b(?:our )?next question\b", r"\bthis concludes\b", r"\boperator instructions?\b",
    r"\bmaybe a higher level strategic question\b", r"\bi will begin with comments\b", r"\bto close,?\b", r"\bturn it over to\b",
    r"\b(?:i was|we were) (?:just |maybe )?hoping\b", r"\bjust how much\b", r"\bhoping you could\b",
    r"\b(?:first|second|third) question\b", r"\bjust to give .* guidance\b", r"\bwhat do you need to do\b",
    r"\b(?:what are some|how are you looking|understanding you guys)\b",
    r"\bnow i would like to turn\b", r"\bnow turning to (?:our )?outlook\b",
    r"\bsuggests to me that maybe\b", r"\bmaybe net new customers\b",
    r"\bfor the sake of re-repeating myself\b", r"\bfirst of all, we look at everything\b",
    r"\bmore in large,? we work discounting\b", r"\bmoving to our non-gaap results\b",
    r"\bwhat we talked to with investors\b", r"\bsmart calculation\b",
    r"^and then online,? maybe\b", r"^it's product diversification,?\b",
    r"\btrying to square\b", r"\bi(?:'d| would) like to follow up\b",
    r"\bworking on churn, churn\b",
    r"\bthe big picture is to continue\b", r"\bhow to accelerate the .* portfolio\b",
    r"\bframed buyback\b", r"^turning to (?:the )?(?:guidance|outlook)\.?$",
)
_MATERIAL_PATTERNS = (
    r"\b(?:increase|increased|increasing|decrease|decreased|decreasing|grew|growth|declin|higher|lower|stronger|weaker|improv|accelerat|decelerat|stable|unchanged|flat)\b",
    r"\b(?:expect|expected|expects|guidance|outlook|forecast|target|plan(?:ned|s|ning)?|goal|rais(?:e|ed|ing)|lower(?:ed|ing)|cut)\b",
    r"\b(?:reported|generated|delivered|achieved|initiated|submitted|approved|launched|built|expanded|acquired|repurchased)\b",
    r"\b(?:revenue|sales|margin|expenses?|cash flow|customers?|bookings?|ARR)\b.{0,45}\b(?:generated|reported|grew|increased|decreased|rose|declined)\b",
    r"\b(?:revenue|sales|margin|expenses?|cash flow|customers?|bookings?|ARR)\b.{0,45}\b(?:was|were|has|have)\b.{0,20}(?:\$|\d|strong|stable|up|down|flat)\b",
    r"\b(?:driven by|due to|because of|reflects|as a result|resulting in)\b",
    r"\b(?:risk|headwind|challenge|uncertain|opportunit|tailwind|potential)\b",
    r"\b(?:launch|launched|approval|approved|acqui(?:re|red|sition)|repurchas|buyback|dividend|invest|build|expand|advance|priorit|capacity|demand|churn|retention|submit|milestone)\b",
    r"\b(?:platform|product|solution|feature)\b.{0,45}\b(?:launch|launched|extend|expanded|introduced|added|deliver|offer|scale|monetiz|adopt)\b",
    r"\b(?:platform|product|solution)\b.{0,50}\b(?:power|extend|serve|enable|connect|system)\b",
    r"\b(?:market share|competitive advantage|differentiated|displac(?:e|ed|ing))\b",
    r"\b(?:customer|client|enterprise)\b.{0,45}\b(?:grew|growth|added|expanded|landed|adopt|churn|retention|demand|purchas|deal|contract)\b",
    r"(?:\$\d|\d+(?:\.\d+)?\s*(?:%|bps|million|billion|thousand)\b)",
)

_DIRECTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("raised", (r"\brais(?:e|ed|ing)\b.{0,40}\b(?:guidance|outlook|forecast|target)\b", r"\b(?:guidance|outlook|forecast|target)\b.{0,40}\b(?:rais(?:e|ed|ing)|higher|increase)\b")),
    ("lowered", (r"\blower(?:ed|ing)\b.{0,40}\b(?:guidance|outlook|forecast|target)\b", r"\b(?:guidance|outlook|forecast|target)\b.{0,40}\b(?:lower|cut|reduc)")),
    ("improving", (r"improv(?:e|ed|ement|ing)", r"better", r"stronger", r"recovery")),
    ("deteriorating", (r"deteriorat", r"weaker", r"pressure", r"headwind", r"declin", r"challeng")),
    ("increasing", (r"increas", r"grew", r"growth", r"higher", r"up \d", r"expanded", r"added")),
    ("decreasing", (r"decreas", r"declin", r"lower", r"down \d", r"fell", r"reduced")),
    ("accelerating", (r"accelerat",)),
    ("decelerating", (r"decelerat", r"slower")),
    ("positive", (r"positive", r"optimistic", r"encouraging", r"strength")),
    ("negative", (r"negative", r"pessimistic", r"unfavorable")),
    ("stable", (r"stable", r"unchanged", r"consistent", r"flat")),
)


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """Split text while preserving exact source offsets."""
    results: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|$)", text, flags=re.S):
        value = match.group(0).strip()
        if not value:
            continue
        left = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
        right = left + len(value)
        results.append((value, left, right))
    return results


def matching_rules(text: str) -> list[ExtractionRule]:
    if not is_material_sentence(text):
        return []
    return [rule for rule in RULES if any(re.search(pattern, text, re.I) for pattern in rule.patterns)]


def is_transcript_noise(text: str) -> bool:
    """Identify boilerplate, logistics, and question-only transcript spans."""
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped or stripped[0].islower() or stripped[0] in ",;:" or re.match(r"^\d", stripped):
        return True
    if "?" in stripped:
        return True
    if any(re.search(pattern, lowered, re.I) for pattern in _NOISE_PATTERNS):
        return True
    if re.match(r"^(?:can|could|would|what|how|why|where|when|who|i(?:'m| am) just wondering|i wonder)\b", lowered):
        return True
    if re.match(r"^(?:maybe|so maybe|to me|if helpful|and kind of|and then i think|i was .*hoping|i'm just wondering)\b", lowered):
        return True
    question_framing = re.search(r"\b(?:you guys|you mentioned|could you|can you|would you|how do you|how much|what are|what do you|why did|where do|kind of|sort of)\b", lowered)
    substantive_answer = re.search(r"\b(?:reported|generated|grew|increased|decreased|expect|guidance|approved|launched|acquired|authorized|raised|lowered|driven|resulted|revenue|sales|margin|demand|bookings|churn|repurchase|buyback|dividend|capacity|FDA|data)\b", lowered)
    if question_framing and not substantive_answer:
        return True
    return False


def is_material_sentence(text: str) -> bool:
    """Require a research-relevant proposition in addition to a keyword."""
    if is_transcript_noise(text):
        return False
    lowered = text.lower()
    if len(re.findall(r"\b[\w'-]+\b", text)) < 4:
        return False
    if re.search(r"\b(?:continue to )?(?:focus|work|look) (?:on|at) (?:our )?(?:product|platform|strategy)\b", lowered) and not re.search(r"\b(?:launch|growth|revenue|sales|customer|demand|margin|capacity|invest|build|expand)\b", lowered):
        return False
    if re.search(r"\b(?:because of|due to)\s+(?:a|the)\s+(?:product|feature|thing)\b", lowered) and not re.search(r"\b(?:growth|revenue|sales|customer|demand|margin|launch|adopt|scale)\b", lowered):
        return False
    return any(re.search(pattern, lowered, re.I) for pattern in _MATERIAL_PATTERNS)


def primary_rule(text: str, rules: list[ExtractionRule]) -> ExtractionRule:
    """Collapse one source sentence to one primary proposition."""
    names = {rule.dimension for rule in rules}
    if "guidance" in names:
        actual_result = re.search(r"\b(?:result|revenue|sales|income|expenses?|margin)\b.{0,45}\b(?:was|were|grew|increased|decreased|above|below|versus|year over year|in line)\b", text, re.I)
        metric = next((rule for rule in rules if rule.dimension in {"revenue", "margins", "costs", "operating_expenses", "cash_flow", "capex", "demand", "pricing", "volume"}), None)
        if actual_result and metric is not None and not re.search(r"\b(?:we expect|we are (?:updating|raising|lowering)|new guidance|guidance range|outlook assumes)\b", text, re.I):
            return metric
        return ExtractionRule("guidance", metric.topic if metric else "outlook", ())
    for dimension in _PRIMARY_ORDER:
        for rule in rules:
            if rule.dimension == dimension:
                return rule
    return rules[0]


def infer_direction(text: str) -> str:
    lowered = text.lower()
    # An actual result above/below guidance is a performance comparison, not
    # a change to the guidance itself.
    actual_vs_guidance = re.search(r"\b(?:result|income|revenue|sales|EPS|earnings)\b.{0,50}\b(?:above|below)\b.{0,30}\b(?:guidance|outlook|forecast)\b", lowered)
    if actual_vs_guidance:
        return "increasing" if re.search(r"\babove\b", actual_vs_guidance.group(0)) else "decreasing"
    matches: list[str] = []
    for direction, patterns in _DIRECTION_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            matches.append(direction)
    if len(matches) > 1 and {"increasing", "decreasing"}.issubset(matches):
        return "mixed"
    return matches[0] if matches else "unknown"


def infer_certainty(text: str, dimension: str) -> str:
    lowered = text.lower()
    if dimension == "risks" or re.search(r"\b(risk|may|might|could|uncertain|uncertainty|subject to)\b", lowered):
        return "risk" if dimension == "risks" or re.search(r"\b(risk|uncertain|uncertainty)\b", lowered) else "possibility"
    if re.search(r"\b(?:result|revenue|sales|income|expenses?|margin)\b.{0,45}\b(?:was|were|grew|increased|decreased|above|below|versus|year over year|in line)\b", lowered) and not re.search(r"\b(?:we expect|we are (?:updating|raising|lowering)|new guidance|outlook assumes)\b", lowered):
        return "actual"
    if re.search(r"\bif approved\b|\bwould\b|\bpotential to\b|\bmay\b|\bmight\b|\bcould\b", lowered):
        return "possibility"
    if re.search(r"\b(?:approved|launched|acquired|initiated|submitted|authorized|completed|closed)\b", lowered):
        return "confirmed"
    if re.search(r"\b(guidance|outlook|forecast|expect(?:ed|s|ing)?|plan(?:s|ned|ning)?|target)\b", lowered):
        return "guidance" if dimension == "guidance" or "guidance" in lowered or "outlook" in lowered else "expectation"
    if re.search(r"\b(estimate|approximately|roughly|about)\b", lowered):
        return "estimate"
    if re.search(r"\b(will|intend|goal|target)\b", lowered):
        return "target"
    if re.search(r"\b(reported|delivered|increased|decreased|grew|declined)\b", lowered):
        return "actual"
    return "unknown"


def infer_magnitude(text: str) -> dict[str, Any] | None:
    """Capture explicit numeric quantities without interpreting them."""
    # Require a currency marker or an explicit unit.  This avoids treating
    # fiscal years and dates as business magnitudes.
    matches = list(re.finditer(r"(?P<value>\$\d+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<unit>\s*%|\s*(?:bps|basis points|million|billion|thousand|M|B|K)\b)?", text, re.I))
    matches = [m for m in matches if m.group("value").startswith("$") or m.group("unit")]
    if not matches:
        return None
    values: list[dict[str, Any]] = []
    for match in matches[:3]:
        raw = match.group(0).strip()
        value = match.group("value").replace("$", "")
        try:
            numeric: int | float = int(float(value)) if float(value).is_integer() else float(value)
        except ValueError:
            continue
        unit = (match.group("unit") or "").strip().lower()
        if raw.startswith("$"):
            unit = "USD" if not unit else f"USD_{unit.replace(' ', '_')}"
        elif unit == "%":
            unit = "percent"
        values.append({"raw": raw, "value": numeric, "unit": unit or None})
    if not values:
        return None
    return values[0] if len(values) == 1 else {"values": values}


def candidate_for_sentence(sentence: str, rule: ExtractionRule) -> ClaimCandidate:
    direction = infer_direction(sentence)
    certainty = infer_certainty(sentence, rule.dimension)
    return ClaimCandidate(
        dimension=rule.dimension,
        topic=rule.topic,
        subtopic=None,
        claim_text=sentence,
        direction=direction if direction in DIRECTION_VALUES else "unknown",
        magnitude=infer_magnitude(sentence),
        certainty=certainty if certainty in CERTAINTY_VALUES else "unknown",
        evidence_text=sentence,
        extraction_method="deterministic_rules_v8",
        extraction_confidence=0.84 if direction != "unknown" else 0.72,
    )



