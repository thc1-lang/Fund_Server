import csv

import pytest

from src.schema import REQUIRED_HEADERS
from src.zacks_importer import _manual_gate, read_and_validate_csv


def test_normal_recaptcha_footer_is_not_a_manual_gate():
    class Scope:
        def __init__(self, text):
            self.text = text

        def locator(self, selector):
            assert selector == "body"
            return self

        def inner_text(self, timeout):
            return self.text

    assert not _manual_gate(Scope("Stock Screener. This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply."))
    assert _manual_gate(Scope("Please verify you are human to continue."))


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
