"""Workbook identities, configuration parsing, and shared credential discovery."""

from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Workbook:
    kind: str
    spreadsheet_id: str
    title: str
    output_tab: str
    output_id: int
    control_key: str
    source_tab: str = "Final scores data"
    control_tab: str = "Control Panel"
    dataset: str = "coincident"

    @property
    def key(self):
        return f"{self.dataset}_{self.kind}"

    @property
    def macro_tab(self):
        return {"coincident": "Coincident Score", "leading": "Leading Score"}[
            self.dataset
        ]


WORKBOOKS = (
    Workbook(
        "correlation",
        "1h-70prpKWYT7Cstg8CR6VRJdfJXaVaxeJL2xX5cXK_k",
        "Correl Score - COIN",
        "Correlation Score",
        1400973068,
        "Correlation Window",
    ),
    Workbook(
        "spread",
        "1TpE-sMW3mxdeXfWavdSfK2EIkz_WzbQbpc0AyjAJXPo",
        "Spread Score - COIN",
        "Spread Score",
        0,
        "Spread Z-Score Window",
    ),
)


def credentials_path(explicit=None):
    configured = explicit or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ValueError(f"Credential file does not exist: {path}")
        return path
    for parent in (ROOT, *ROOT.parents):
        path = parent / "google_credentials.json"
        if path.is_file():
            return path
    raise ValueError(
        "Set GOOGLE_APPLICATION_CREDENTIALS or --credentials-file to the existing Google credential file."
    )


def window_value(rows, key):
    found = [r[1] for r in rows if r and r[0] == key and len(r) > 1]
    if len(found) != 1:
        raise ValueError(
            f"Control Panel must contain exactly one {key!r} setting in columns A:B"
        )
    v = found[0]
    if isinstance(v, bool):
        raise ValueError(f"{key} must be an integer >= 2")
    try:
        n = float(v)
        if not n.is_integer() or n < 2:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{key} must be an integer >= 2; got {v!r}") from None
    return int(n)


BASE_WORKBOOKS = WORKBOOKS
MOMENTUM_WORKBOOKS = (
    Workbook(
        "spread_momentum",
        "1PPVvOClJ3Ttk5We81pDIQyrYKtywuWuxhFITOulfXZA",
        "Spread Momentum Score",
        "Spread Momentum Score",
        0,
        "Momentum Z-Score Window",
        "Imported Spread Data",
    ),
    Workbook(
        "correlation_momentum",
        "1ZoApVQIQmm0UUuG4A1s1CJ-b7ITbql5eWwZYvrsb8zc",
        "Correlation Momentum Score",
        "Correlation Momentum Score",
        1400973068,
        "Momentum Z-Score Window",
        "Imported Correlation Data",
    ),
)
COINCIDENT_WORKBOOKS = BASE_WORKBOOKS + MOMENTUM_WORKBOOKS
LEADING_BASE_WORKBOOKS = (
    Workbook(
        "correlation",
        "1Ma0pirU2pmMSFgZXKB4v5y1gw9W8vzBI8xt6tPxiz90",
        "Correl Score - LEAD",
        "Correlation Score",
        1400973068,
        "Correlation Window",
        dataset="leading",
    ),
    Workbook(
        "spread",
        "1sOkwKFe0d42NC7phUkGGOrhhwhDoff78rlQCL-1GUt0",
        "Spread Score - LEAD",
        "Spread Score",
        0,
        "Spread Z-Score Window",
        dataset="leading",
    ),
)
LEADING_MOMENTUM_WORKBOOKS = (
    Workbook(
        "spread_momentum",
        "18uNC1bykf_a-F9tHTpgCgPk2e_Bw3TPMsJ6BQuZiL4g",
        "Spread Momentum Score - LEAD",
        "Spread Momentum Score",
        0,
        "Momentum Z-Score Window",
        "Imported Spread Data",
        dataset="leading",
    ),
    Workbook(
        "correlation_momentum",
        "1b-TWGIzqmzfG76ssiMAwROdCQQh7fhXEh2u4NTPRCvA",
        "Correlation Momentum Score - LEAD",
        "Correlation Momentum Score",
        1400973068,
        "Momentum Z-Score Window",
        "Imported Correlation Data",
        dataset="leading",
    ),
)
LEADING_WORKBOOKS = LEADING_BASE_WORKBOOKS + LEADING_MOMENTUM_WORKBOOKS
BASE_WORKBOOKS += LEADING_BASE_WORKBOOKS
MOMENTUM_WORKBOOKS += LEADING_MOMENTUM_WORKBOOKS
WORKBOOKS = COINCIDENT_WORKBOOKS + LEADING_WORKBOOKS
UPSTREAM = {(b.dataset, b.kind + "_momentum"): b for b in BASE_WORKBOOKS}


def upstream_book(book):
    return UPSTREAM[(book.dataset, book.kind)]


def select_workbooks(dataset="all", kind=None, momentum_only=False):
    if dataset not in ("all", "coincident", "leading"):
        raise ValueError(f"Unknown dataset: {dataset}")
    if kind is not None and kind not in {b.kind for b in WORKBOOKS}:
        raise ValueError(f"Unknown calculation: {kind}")
    return tuple(
        b
        for b in WORKBOOKS
        if (dataset == "all" or b.dataset == dataset)
        and (kind is None or b.kind == kind)
        and (not momentum_only or b.kind.endswith("_momentum"))
    )


def momentum_lookback(rows):
    # Reuse integer validation with a shifted lower bound; no hard-coded setting.
    values = [r[1] for r in rows if r and r[0] == "Momentum Lookback" and len(r) > 1]
    if len(values) != 1 or isinstance(values[0], bool):
        raise ValueError("Momentum Lookback must occur once and be an integer >= 1")
    try:
        n = float(values[0])
        if not n.is_integer() or n < 1:
            raise ValueError()
        return int(n)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Momentum Lookback must be an integer >= 1") from None
