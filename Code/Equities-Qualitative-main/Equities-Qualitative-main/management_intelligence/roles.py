"""Conservative role classification for current and historical titles."""

from __future__ import annotations

import re


ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chair", re.compile(r"\b(?:chair(?:man|person)?|chair of the board)\b", re.I)),
    ("lead_independent_director", re.compile(r"\blead independent director\b", re.I)),
    ("director", re.compile(r"\bdirector\b|\bboard member\b", re.I)),
    ("CEO", re.compile(r"\bchief executive officer\b|\bCEO\b|principal executive officer", re.I)),
    ("CFO", re.compile(r"\bchief financial officer\b|\bCFO\b|principal financial officer", re.I)),
    ("COO", re.compile(r"\bchief operating officer\b|\bCOO\b", re.I)),
    ("president", re.compile(r"\bpresident\b", re.I)),
    ("CTO", re.compile(r"\bchief technology officer\b|\bCTO\b", re.I)),
    ("chief_product_officer", re.compile(r"\bchief product officer\b|\bCPO\b", re.I)),
    ("chief_commercial_officer", re.compile(r"\bchief commercial officer\b|\bCCO\b", re.I)),
    ("general_counsel", re.compile(r"\bgeneral counsel\b|\bchief legal officer\b", re.I)),
    ("founder", re.compile(r"\bco[- ]?founder\b|\bfounder\b", re.I)),
    ("executive", re.compile(r"\bchief\b|\bexecutive officer\b|\bsecretary\b|\btreasurer\b|\bvice president\b", re.I)),
)


def classify_role(title: str | None) -> list[str]:
    text = " ".join(str(title or "").split())
    categories: list[str] = []
    for category, pattern in ROLE_PATTERNS:
        if pattern.search(text) and category not in categories:
            categories.append(category)
    return categories


def is_key_role(title: str | None) -> bool:
    return bool(set(classify_role(title)) & {"CEO", "CFO", "COO", "president", "CTO", "chief_product_officer", "chief_commercial_officer", "general_counsel", "chair", "lead_independent_director", "director", "founder", "executive"})


def responsibilities_from_title(title: str | None) -> list[str]:
    categories = classify_role(title)
    mapping = {
        "CEO": "company-wide executive leadership",
        "CFO": "finance and financial reporting",
        "COO": "operations",
        "president": "business or company operations",
        "CTO": "technology and engineering",
        "chief_product_officer": "product strategy and development",
        "chief_commercial_officer": "commercial and go-to-market activities",
        "general_counsel": "legal and regulatory matters",
        "chair": "board leadership",
        "lead_independent_director": "independent board leadership",
        "director": "board oversight",
    }
    return [mapping[item] for item in categories if item in mapping]


__all__ = ["classify_role", "is_key_role", "responsibilities_from_title"]
