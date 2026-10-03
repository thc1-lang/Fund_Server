"""Rebuild the standalone Python/Colab artifact from the maintained source files."""

from pathlib import Path
import ast
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable"
RUNTIME = (
    "run_us_pipeline.py",
    "monthly_indicators.py",
    "us_indicator_analysis.py",
    "run_relationships.py",
    "config.py",
    "google_sheets.py",
    "pipeline.py",
    "momentum_pipeline.py",
    "calculation_helper.py",
    "validation.py",
    "spread.py",
    "correlation.py",
    "momentum.py",
    "source_freshness.py",
    "run_lock.py",
    "requirements.txt",
)
HEADER = """# US complete pipeline — paste this ENTIRE file into ONE Google Colab code cell.
# This includes every runtime module; it does not download your project code.
# Run the cell to publish data and all scores. Google credentials AND a FRED key
# are required. Secrets may also be configured in Colab Secrets using these names:
# GOOGLE_APPLICATION_CREDENTIALS (path), FRED_API_KEY,
# TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN (optional notification credentials).
# The credential JSON must be uploaded in each fresh runtime.
# Existing workbook IDs/structures and authorised imports are required.
# Edit STAGE to run one part independently; "all" runs everything in order.
DATASET = "all"  # all, coincident, leading; each has separate workbooks
STAGE = "all"  # importer, analysis, relationships, spread, correlation,
               # momentum, spread-momentum, correlation-momentum

import os
import sys
from pathlib import Path

SOURCES = {}
"""
FOOTER = '''
def materialize(destination):
    """Extract the exact embedded sources; does not execute or contact services."""
    import hashlib
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in SOURCES.items():
        if hashlib.sha256(source.encode()).hexdigest() != SOURCE_HASHES[name]:
            raise ValueError("Embedded source integrity check failed: " + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return destination


def run_embedded(notebook=False):
    import getpass
    import subprocess
    import tempfile
    if notebook:
        destination = Path.cwd() / "us_complete_pipeline_runtime"
    else:
        artifact_folder = Path(__file__).resolve().parent
        if artifact_folder.name == "portable" and (artifact_folder.parent / "run_us_pipeline.py").is_file():
            artifact_folder = artifact_folder.parent
        destination = artifact_folder / "work" / "standalone_runtime"
    materialize(destination)
    environment = dict(os.environ)
    if notebook:
        try:
            from google.colab import userdata
            for key in ("FRED_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN"):
                if not environment.get(key):
                    try:
                        value = userdata.get(key)
                        if value: environment[key] = value
                    except Exception: pass
        except ImportError: pass
        credential = environment.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not credential or not Path(credential).expanduser().is_file():
            from google.colab import files
            print("Upload the Google service-account JSON used for these workbooks.")
            secret_dir = Path(tempfile.mkdtemp(prefix="us_google_credentials_"))
            original = Path.cwd()
            try:
                os.chdir(secret_dir)
                uploaded = files.upload()
            finally:
                os.chdir(original)
            candidates = [secret_dir / Path(name).name for name in uploaded if name.lower().endswith(".json")]
            if len(candidates) != 1:
                raise ValueError("Upload exactly one Google credential JSON file")
            credential = str(candidates[0])
            Path(credential).chmod(0o600)
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(Path(credential).expanduser().resolve())
        if STAGE in ("all", "importer") and not environment.get("FRED_API_KEY"):
            environment["FRED_API_KEY"] = getpass.getpass("FRED API key: ").strip()
        if STAGE in ("all", "importer") and not environment.get("FRED_API_KEY"):
            raise ValueError("A FRED API key is required")
        arguments = ["--stage", STAGE, "--dataset", DATASET]
    else:
        arguments = sys.argv[1:] or ["--stage", STAGE, "--dataset", DATASET]
    print("Extracted complete pipeline to", destination, flush=True)
    child = subprocess.Popen([sys.executable, "-u", str(destination / "run_us_pipeline.py"), *arguments], env=environment)
    try:
        code = child.wait()
    except KeyboardInterrupt:
        child.terminate()
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
        raise
    if code:
        raise RuntimeError(f"Pipeline stopped with status {code}. Inspect the stage output above and {destination / 'work'}.")


if __name__ == "__main__":
    run_embedded(notebook="get_ipython" in globals())
'''


def build():
    PORTABLE.mkdir(exist_ok=True)
    files = (
        list(RUNTIME)
        + [
            "notifications/__init__.py",
            "notifications/telegram.py",
            "notifications/lifecycle.py",
            "notifications/changes.py",
            "notifications/control.py",
        ]
        + [
            p.relative_to(ROOT).as_posix()
            for p in sorted((ROOT / "tests").glob("*.py"))
            if not p.name.startswith("._")
        ]
    )
    chunks = [HEADER]
    hashes = {}
    for name in files:
        source = (ROOT / name).read_text(encoding="utf-8")
        hashes[name] = hashlib.sha256(source.encode()).hexdigest()
        # repr per source line preserves exact backslashes, Unicode and indentation.
        # Every source line remains individually visible and editable in the cell.
        literal = (
            "(\n"
            + "".join(
                "    " + repr(line) + "\n" for line in source.splitlines(keepends=True)
            )
            + ")"
        )
        assert ast.literal_eval(literal) == source
        chunks.append(
            "\n# Embedded source: "
            + name
            + "\nSOURCES["
            + repr(name)
            + "] = "
            + literal
            + "\n"
        )
    chunks.append("\nSOURCE_HASHES = " + repr(hashes) + "\n" + FOOTER)
    script = "".join(chunks)
    compile(script, "US_PIPELINE_COLAB.py", "exec")
    (PORTABLE / "US_PIPELINE_COLAB.py").write_text(script, encoding="utf-8")
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "cells": [
            {
                "cell_type": "code",
                "id": "us-complete-pipeline",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": script.splitlines(keepends=True),
            }
        ],
    }
    (PORTABLE / "US_PIPELINE_COLAB.ipynb").write_text(
        json.dumps(notebook, indent=1), encoding="utf-8"
    )
    build_archive(files)
    print(
        f"Built standalone cell: {len(script.splitlines()):,} lines, {len(script.encode()):,} bytes; {len(files)} embedded files"
    )


def build_archive(embedded_files):
    """Package an explicit file allowlist: no credentials, environments or logs."""
    names = set(embedded_files)
    names.update(
        (
            "README.md",
            "Start.command",
            "Start.bat",
            ".gitignore",
            ".editorconfig",
            "pyproject.toml",
            "requirements-dev.txt",
            "portable/README.md",
            "portable/US_PIPELINE_COLAB.py",
            "portable/US_PIPELINE_COLAB.ipynb",
        )
    )
    for folder, pattern in (("docs", "*.md"), ("tools", "*.py")):
        names.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / folder).glob(pattern)
            if not path.name.startswith("._")
        )
    archive_path = PORTABLE / "us_complete_pipeline_portable.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            archive.write(ROOT / name, "us_complete_pipeline/" + name)
    with zipfile.ZipFile(archive_path) as archive:
        damaged = archive.testzip()
        if damaged:
            raise ValueError("Archive integrity check failed: " + damaged)
    print(f"Built portable ZIP: {len(names)} files -> {archive_path}")


if __name__ == "__main__":
    build()
