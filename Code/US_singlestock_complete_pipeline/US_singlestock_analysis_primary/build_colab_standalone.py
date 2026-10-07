"""Build the complete application as one self-contained Python source file."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT
BUNDLE_ROOT = ROOT / "US_single_stock_colab_bundle"
OUTPUTS = (
    ROOT / "US_Single_Stock_Colab_Standalone.py",
    BUNDLE_ROOT / "US_Single_Stock_Colab_Standalone.py",
)

MODULE_FILES = {
    "config": SOURCE_ROOT / "config.py",
    "src": SOURCE_ROOT / "src" / "__init__.py",
    "src.economic_policy": SOURCE_ROOT / "src" / "economic_policy.py",
    "src.shorts": SOURCE_ROOT / "src" / "shorts.py",
    "src.audit": SOURCE_ROOT / "src" / "audit.py",
    "src.cleaner": SOURCE_ROOT / "src" / "cleaner.py",
    "src.institutional": SOURCE_ROOT / "src" / "institutional.py",
    "src.normalization": SOURCE_ROOT / "src" / "normalization.py",
    "src.peer_statistics": SOURCE_ROOT / "src" / "peer_statistics.py",
    "src.ranking": SOURCE_ROOT / "src" / "ranking.py",
    "src.schema": SOURCE_ROOT / "src" / "schema.py",
    "src.scoring": SOURCE_ROOT / "src" / "scoring.py",
    "src.sheets_reader": SOURCE_ROOT / "src" / "sheets_reader.py",
    "src.sheets_writer": SOURCE_ROOT / "src" / "sheets_writer.py",
    "src.zacks_importer": SOURCE_ROOT / "src" / "zacks_importer.py",
    "src.strategies": SOURCE_ROOT / "src" / "strategies.py",
    "src.transformations": SOURCE_ROOT / "src" / "transformations.py",
    "src.universe": SOURCE_ROOT / "src" / "universe.py",
    "src.validation": SOURCE_ROOT / "src" / "validation.py",
}

HEADER = '''#!/usr/bin/env python3
"""US Single-Stock Institutional Scoring System — standalone Colab edition.

This single file embeds the complete application. It does not contain a workbook
ID or Google credentials. Each user must supply their own private workbook and
service-account JSON file.

Examples:
  python US_Single_Stock_Colab_Standalone.py --input-xlsx workbook.xlsx
  python US_Single_Stock_Colab_Standalone.py --live \\
      --spreadsheet-id YOUR_SHEET_ID --credentials-file service_account.json
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import os
import subprocess
import sys
import types

STANDALONE_VERSION = "primary_snapshot_v7"


def _install_missing_dependencies() -> None:
    """Install only packages missing from the current Python environment."""
    if os.getenv("US_STOCK_SKIP_DEPENDENCY_CHECK") == "1":
        return
    checks = {
        "pandas": "pandas>=2.2,<4",
        "numpy": "numpy>=1.26,<3",
        "openpyxl": "openpyxl>=3.1,<4",
        "pyarrow": "pyarrow>=17,<26",
        "gspread": "gspread>=6.1,<7",
        "google.auth": "google-auth>=2.34,<3",
        "requests": "requests>=2.32,<3",
    }
    missing = []
    for module_name, requirement in checks.items():
        try:
            __import__(module_name)
        except ImportError:
            missing.append(requirement)
    if missing:
        print("Installing missing dependencies: " + ", ".join(missing), file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *missing])


def _extract_portable_arguments() -> None:
    """Translate standalone connection arguments into the application's environment."""
    cleaned = [sys.argv[0]]
    index = 1
    while index < len(sys.argv):
        argument = sys.argv[index]
        if argument in {"--spreadsheet-id", "--credentials-file"}:
            if index + 1 >= len(sys.argv):
                raise SystemExit(f"{argument} requires a value")
            value = sys.argv[index + 1]
            if argument == "--spreadsheet-id":
                os.environ["SOURCE_SPREADSHEET_ID"] = value
                os.environ["DESTINATION_SPREADSHEET_ID"] = value
            else:
                os.environ["GOOGLE_CREDENTIALS_FILE"] = value
            index += 2
            continue
        if argument.startswith("--spreadsheet-id="):
            value = argument.split("=", 1)[1]
            os.environ["SOURCE_SPREADSHEET_ID"] = value
            os.environ["DESTINATION_SPREADSHEET_ID"] = value
        elif argument.startswith("--credentials-file="):
            os.environ["GOOGLE_CREDENTIALS_FILE"] = argument.split("=", 1)[1]
        else:
            cleaned.append(argument)
        index += 1
    sys.argv[:] = cleaned


class _EmbeddedLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        source = EMBEDDED_SOURCES[module.__name__]
        module.__file__ = f"<standalone:{module.__name__}>"
        if module.__name__ == "src":
            module.__path__ = []
            module.__package__ = "src"
        exec(compile(source, module.__file__, "exec"), module.__dict__)


class _EmbeddedFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in EMBEDDED_SOURCES:
            return None
        return importlib.util.spec_from_loader(
            fullname, _EmbeddedLoader(), is_package=(fullname == "src")
        )


def _run_application() -> None:
    _extract_portable_arguments()
    if not any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        local_mode = any(arg.split("=", 1)[0] in {"--input-xlsx", "--input-snapshot"} for arg in sys.argv[1:])
        if not local_mode and not os.getenv("SOURCE_SPREADSHEET_ID"):
            raise SystemExit(
                "Supply --spreadsheet-id YOUR_SHEET_ID, or use --input-xlsx workbook.xlsx"
            )
    _install_missing_dependencies()
    namespace = {
        "__name__": "__main__",
        "__file__": "<standalone:main>",
        "__package__": None,
    }
    exec(compile(EMBEDDED_MAIN, namespace["__file__"], "exec"), namespace)

'''

FOOTER = '''

_embedded_finder = _EmbeddedFinder()
sys.meta_path.insert(0, _embedded_finder)

if __name__ == "__main__":
    _run_application()
'''


def build() -> str:
    sources = {name: path.read_text(encoding="utf-8") for name, path in MODULE_FILES.items()}
    # The portable artifact must not embed the owner's workbook identifier.
    import re
    sources["config"] = re.sub(r'SOURCE_SPREADSHEET_ID = os.getenv\("SOURCE_SPREADSHEET_ID", "[^"]*"\)',
        'SOURCE_SPREADSHEET_ID = os.getenv("SOURCE_SPREADSHEET_ID", "")', sources["config"])
    main_source = (SOURCE_ROOT / "main.py").read_text(encoding="utf-8")
    payload = HEADER
    payload += "\nEMBEDDED_SOURCES = " + repr(sources) + "\n"
    payload += "\nEMBEDDED_MAIN = " + repr(main_source) + "\n"
    payload += FOOTER
    for output in OUTPUTS:
        output.write_text(payload, encoding="utf-8")
    # Keep the current unpacked distribution on the same source as the single file.
    for name, source in sources.items():
        relative = "src/__init__.py" if name == "src" else name.replace(".", "/") + ".py"
        destination = BUNDLE_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    (BUNDLE_ROOT / "main.py").write_text(main_source, encoding="utf-8")
    for name in ("requirements.txt", "pytest.ini", "README.md"):
        (BUNDLE_ROOT / name).write_text((ROOT / name).read_text(), encoding="utf-8")
    for test in (ROOT / "tests").glob("test_*.py"):
        (BUNDLE_ROOT / "tests").mkdir(exist_ok=True)
        (BUNDLE_ROOT / "tests" / test.name).write_text(test.read_text(), encoding="utf-8")
    return payload


if __name__ == "__main__":
    built = build()
    print(f"Built {len(built):,} characters")
    for output in OUTPUTS:
        print(output)
