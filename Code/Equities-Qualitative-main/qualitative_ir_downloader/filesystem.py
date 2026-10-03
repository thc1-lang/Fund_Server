"""Windows-safe naming and atomic output helpers."""
import json
import re
import time
from pathlib import Path
from typing import Any

def sanitize_filename(value: str, max_length: int = 120) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")[:max_length].rstrip(" .") or "untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value[:max_length]

def company_folder(root: Path, ticker: str, name: str, timestamp: str) -> Path:
    stem = f"{sanitize_filename(ticker, 16)}_{sanitize_filename(name, 55)}_qualitative analysis_{timestamp}"
    folder = root / stem
    index = 2
    while folder.exists():
        folder = root / f"{stem}_{index}"
        index += 1
    folder.mkdir(parents=True)
    for sub in ("News Releases", "Reports", "Events", "logs"):
        (folder / sub).mkdir()
    return folder

def document_path(folder: Path, ticker: str, title: str, date: str | None) -> Path:
    budget = min(140, 240 - len(str(folder.resolve())) - 12)
    if budget < 24:
        raise ValueError("Output root is too long; choose a shorter --download-root")
    stem = sanitize_filename(f"{date or 'UNKNOWN-DATE'}_{ticker}_{title}", budget)
    target = folder / f"{stem}.pdf"
    index = 2
    while target.exists():
        target = folder / f"{stem}_{index}.pdf"
        index += 1
    return target

def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    # Windows indexers/scanners may briefly hold a freshly replaced manifest.
    # Retry only sharing/permission failures; retain atomic replacement.
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * 2 ** attempt)
