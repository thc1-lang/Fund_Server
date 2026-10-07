import csv

import pytest

from src.schema import REQUIRED_HEADERS
from src.zacks_importer import read_and_validate_csv


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def test_zacks_csv_is_validated_before_dataset_write(tmp_path):
    path = tmp_path / "zacks.csv"
    headers = sorted(REQUIRED_HEADERS)
    write_csv(path, [headers, ["value"] * len(headers)])
    assert read_and_validate_csv(path) == [headers, ["value"] * len(headers)]


def test_zacks_csv_rejects_missing_or_ragged_schema(tmp_path):
    missing = tmp_path / "missing.csv"
    write_csv(missing, [["Ticker"], ["ABC"]])
    with pytest.raises(ValueError, match="missing required headers"):
        read_and_validate_csv(missing)
    ragged = tmp_path / "ragged.csv"
    headers = sorted(REQUIRED_HEADERS)
    write_csv(ragged, [headers, ["value"]])
    with pytest.raises(RuntimeError, match="ragged"):
        read_and_validate_csv(ragged)
