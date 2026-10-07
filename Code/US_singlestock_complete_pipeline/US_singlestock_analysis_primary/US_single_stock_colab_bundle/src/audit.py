from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
import pandas as pd
from .schema import UNAVAILABLE_CALCULATIONS
from .transformations import derived_registry_frame
from .sheets_writer import build_industry_summary, build_quant_summary
from .institutional import availability_registry_frame


def _industry_preview_filename(industry: object, used: set[str]) -> str:
    raw = str(industry)
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw).strip(" ._")[:120] or "UNNAMED_INDUSTRY"
    filename = f"{stem}.csv"
    if filename.casefold() in used:
        filename = f"{stem[:110]}-{sha256(raw.encode('utf-8')).hexdigest()[:8]}.csv"
    used.add(filename.casefold())
    return filename


def write_outputs(df: pd.DataFrame, excluded: pd.DataFrame, manifest, output_dir: str | Path, summary: dict) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    audit_csv = out / "full_audit.csv"
    df.to_csv(audit_csv, index=False)
    paths["audit_csv"] = str(audit_csv)
    quant = out / "quant_summary_preview.csv"
    build_quant_summary(df).to_csv(quant, index=False)
    paths["quant_summary_preview"] = str(quant)
    industry_summary = out / "industry_summary_preview.csv"
    build_industry_summary(df).to_csv(industry_summary, index=False)
    paths["industry_summary_preview"] = str(industry_summary)
    preview_dir = out / "industry_rankings_preview"
    preview_dir.mkdir(exist_ok=True)
    preview_columns = [c for c in [
        "Expanded Long Rank", "Expanded Short Rank",
        "Ticker", "Company Name", "Exchange", "COM/ADR/Canadian", "Sector", "Industry",
        "Expanded Long Score", "Expanded Short Score", "Expanded Net Quant Score",
        "Quantitative Candidate", "Implementation Candidate", "Score Confidence", "Short Score Confidence", "Positive Peer Evidence Count",
        "Negative Peer Evidence Count", "Excessive Working Capital Flag",
        "Implementation Feasibility Note", "Data Status", "Short Data Status",
    ] if c in df]
    used_names: set[str] = set()
    expected_previews: set[Path] = set()
    for industry, group in df.groupby("Industry", sort=True):
        preview_path = preview_dir / _industry_preview_filename(industry, used_names)
        expected_previews.add(preview_path)
        group.sort_values(
            ["Expanded Net Quant Score", "Expanded Long Score"], ascending=[False, False]
        )[preview_columns].to_csv(preview_path, index=False)
    for stale_path in preview_dir.glob("*.csv"):
        if stale_path not in expected_previews:
            stale_path.unlink()
    paths["industry_rankings_preview"] = str(preview_dir)
    try:
        audit_parquet = out / "full_audit.parquet"
        df.to_parquet(audit_parquet, index=False); paths["audit_parquet"] = str(audit_parquet)
    except (ImportError, ValueError):
        summary.setdefault("warnings", []).append("Parquet unavailable; CSV audit was written.")
    excluded_path = out / "excluded_universe.csv"
    excluded.to_csv(excluded_path, index=False)
    paths["excluded_universe"] = str(excluded_path)
    registry_path = out / "derived_feature_registry.csv"
    derived_registry_frame().to_csv(registry_path, index=False)
    paths["derived_feature_registry"] = str(registry_path)
    controls = summary.get("model_controls", {})
    controls_path = out / "model_control_panel.csv"
    pd.DataFrame([
        {"control": key, "value": json.dumps(value) if isinstance(value, (dict, list, tuple)) else value}
        for key, value in controls.items()
    ]).to_csv(controls_path, index=False)
    paths["model_control_panel"] = str(controls_path)
    unavailable_path = out / "unavailable_calculations.csv"
    availability_registry_frame(set(df.columns)).to_csv(unavailable_path, index=False)
    paths["unavailable_calculations"] = str(unavailable_path)
    manifest_path = out / "tab_manifest.csv"
    pd.DataFrame({
        "matched": pd.Series(manifest.matched),
        "missing": pd.Series(manifest.missing),
        "unexpected": pd.Series(manifest.unexpected),
    }).to_csv(manifest_path, index=False)
    paths["tab_manifest"] = str(manifest_path)
    summary["retrieval_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    summary_json = out / "run_summary.json"
    summary_json.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    paths["run_summary_json"] = str(summary_json)
    summary_csv = out / "run_summary.csv"
    pd.DataFrame([summary]).to_csv(summary_csv, index=False)
    paths["run_summary_csv"] = str(summary_csv)
    return paths
