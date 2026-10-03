from dataclasses import replace
from pathlib import Path
import pytest
from config import (
    WORKBOOKS,
    COINCIDENT_WORKBOOKS,
    LEADING_WORKBOOKS,
    LEADING_BASE_WORKBOOKS,
    LEADING_MOMENTUM_WORKBOOKS,
    select_workbooks,
    upstream_book,
)
from google_sheets import Sheets, a1
from momentum_pipeline import desired_formulas, assert_fresh
from source_freshness import wait_for_macro, prepare_dataset_links
from validation import ValidationError

LEAD_IDS = {
    "1sOkwKFe0d42NC7phUkGGOrhhwhDoff78rlQCL-1GUt0",
    "1Ma0pirU2pmMSFgZXKB4v5y1gw9W8vzBI8xt6tPxiz90",
    "1b-TWGIzqmzfG76ssiMAwROdCQQh7fhXEh2u4NTPRCvA",
    "18uNC1bykf_a-F9tHTpgCgPk2e_Bw3TPMsJ6BQuZiL4g",
}


def test_exact_separate_user_supplied_workbooks_and_audit_keys():
    assert {b.spreadsheet_id for b in LEADING_WORKBOOKS} == LEAD_IDS
    assert len({b.spreadsheet_id for b in WORKBOOKS}) == 8
    assert len({b.key for b in WORKBOOKS}) == 8
    assert not LEAD_IDS & {b.spreadsheet_id for b in COINCIDENT_WORKBOOKS}
    assert {b.macro_tab for b in LEADING_WORKBOOKS} == {"Leading Score"}


@pytest.mark.parametrize(
    "dataset,count", [("all", 8), ("coincident", 4), ("leading", 4)]
)
def test_selectors(dataset, count):
    selected = select_workbooks(dataset)
    assert len(selected) == count
    assert all(dataset == "all" or b.dataset == dataset for b in selected)
    for kind in ("spread", "correlation", "spread_momentum", "correlation_momentum"):
        subset = select_workbooks(dataset, kind)
        assert len(subset) == count // 4
        assert all(b.kind == kind for b in subset)
    assert len(select_workbooks(dataset, momentum_only=True)) == count // 2


@pytest.mark.parametrize("book", LEADING_MOMENTUM_WORKBOOKS, ids=lambda b: b.key)
def test_leading_momentum_never_imports_coincident_sources(book):
    up = upstream_book(book)
    assert up.dataset == book.dataset == "leading"
    formulas = desired_formulas(book, 38, 667)
    imports = [f for _, _, f in formulas if "IMPORTRANGE" in f]
    assert imports and all(up.spreadsheet_id in f for f in imports)
    assert not any(
        old.spreadsheet_id in f for old in COINCIDENT_WORKBOOKS for f in imports
    )


@pytest.mark.parametrize("book", COINCIDENT_WORKBOOKS, ids=lambda b: b.key)
def test_leading_only_write_client_rejects_every_coincident_book(book):
    api = Sheets.__new__(Sheets)
    api.write_ids = LEAD_IDS
    with pytest.raises(ValueError, match="prohibited"):
        api.request("POST", book.spreadsheet_id, ":batchUpdate", json={})


@pytest.mark.parametrize("book", LEADING_BASE_WORKBOOKS, ids=lambda b: b.key)
def test_correct_macro_id_but_wrong_dataset_is_rejected(book):
    class Fake:
        def values(self, *a, **kw):
            return [
                [
                    [
                        '=IMPORTRANGE("19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c","\'Coincident Score\'!A1:AL")'
                    ]
                ]
            ]

    with pytest.raises(ValidationError, match="wrong score dataset"):
        wait_for_macro(Fake(), book, attempts=1)


@pytest.mark.parametrize("book", LEADING_MOMENTUM_WORKBOOKS, ids=lambda b: b.key)
def test_momentum_freshness_reads_matching_leading_upstream(book):
    up = upstream_book(book)
    source = [["Date", "X"], [45000, 1.0]]
    pairs = [["Date", "X"], [45000, "Y"]]

    class Fake:
        def values(self, id, ranges):
            assert id == up.spreadsheet_id
            return [source, pairs]

    assert_fresh(Fake(), book, source, pairs)
    with pytest.raises(ValidationError, match="stale"):
        assert_fresh(Fake(), book, [["Date", "X"], [45000, 99.0]], pairs)


class LinkSheets:
    def __init__(self, book):
        self.book = book
        self.posts = []
        self.backups = []
        old = next(b for b in COINCIDENT_WORKBOOKS if b.kind == book.kind)
        self.controls = [["Setting", "Value"], [book.control_key, 24]]
        if book.kind.endswith("_momentum"):
            self.controls += [
                ["Momentum Lookback", 3],
                ["Source Spreadsheet ID", upstream_book(old).spreadsheet_id],
                ["Source Tab", upstream_book(old).output_tab],
            ]
        self.formulas = {
            a1(book.output_tab, "A1"): [
                ['=IMPORTRANGE("old","\'Coincident Score\'!A1:A")']
            ]
        }

    def metadata(self, id):
        assert id == self.book.spreadsheet_id
        return {
            "sheets": [
                {
                    "properties": {
                        "title": self.book.output_tab,
                        "sheetId": self.book.output_id,
                    }
                }
            ]
        }

    def values(self, id, ranges, formulas=False):
        assert id == self.book.spreadsheet_id
        if ranges == [a1(self.book.control_tab, "A1:B20")]:
            return [self.controls]
        return [self.formulas.get(r, []) for r in ranges]

    def backup(self, path, data):
        self.backups.append((path, data))

    def request(self, method, id, suffix, json):
        assert method == "POST" and id in LEAD_IDS
        self.posts.append(json)
        for change in json["data"]:
            self.formulas[change["range"]] = change["values"]


@pytest.mark.parametrize("book", LEADING_WORKBOOKS, ids=lambda b: b.key)
def test_leading_link_repairs_are_scoped_and_backed_up(book, tmp_path, monkeypatch):
    monkeypatch.setattr("source_freshness.wait_for_macro", lambda *a: None)
    api = LinkSheets(book)
    original = [row[:] for row in api.controls]
    prepare_dataset_links(api, book, tmp_path)
    assert api.backups and api.backups[0][0].name == book.key + "_links_before.json.gz"
    assert api.controls == original  # numerical settings are never rewritten
    writes = [r for batch in api.posts for r in batch["data"]]
    if book.kind == "spread":
        assert writes == [
            {
                "range": a1(book.output_tab, "A1"),
                "values": [["=ARRAYFORMULA('Final scores data'!A1:A)"]],
            }
        ]
    elif book.kind.endswith("_momentum"):
        assert any(
            r["values"] == [[upstream_book(book).spreadsheet_id]] for r in writes
        )
        assert all(r["range"].startswith("'Control Panel'!B") for r in writes)
    else:
        assert not writes


def test_existing_coincident_links_are_not_repaired(tmp_path):
    class NoAccess:
        def __getattr__(self, name):
            raise AssertionError("Coincident setup should be unchanged")

    for book in COINCIDENT_WORKBOOKS:
        prepare_dataset_links(NoAccess(), book, tmp_path)


def test_leading_cli_client_cannot_publish_to_original_books(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.lifecycle.send_telegram", lambda *a, **kw: True)
    import run_relationships as main

    seen = {}

    class Fake:
        writes_attempted = False

        def __init__(self, credentials, write_ids):
            seen["ids"] = write_ids

    def interrupt(sheets, book):
        assert book.dataset == "leading"
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "credentials_path", lambda *a: "unused")
    monkeypatch.setattr("source_freshness.validate_macro_summary", lambda *a: None)
    monkeypatch.setattr(main, "Sheets", Fake)
    monkeypatch.setattr("source_freshness.prepare_dataset_links", lambda *a: None)
    monkeypatch.setattr(main, "load", interrupt)
    assert main.main(["--dataset", "leading"]) == 130
    assert seen["ids"] == LEAD_IDS


@pytest.mark.parametrize("expected", ["Coincident Score", "Leading Score"])
def test_summary_selectors_reject_warning_mixup_and_wrong_indicator(expected):
    from source_freshness import check_summary_selectors
    from google_sheets import column_name

    headers = ["Date"] + [f"Indicator {i}" for i in range(37)]
    helpers = [[], []]
    for i in range(37):
        cell = column_name(40 + 2 * i)
        helpers[0].extend(["=" + column_name(i + 2) + "1", ""])
        helpers[1].extend(
            [
                f'=LET(sheet,{cell}1,scoreCol,MATCH("{expected}",INDIRECT("header"),0),scoreCol)',
                "",
            ]
        )
    check_summary_selectors(headers, helpers, expected)
    good = helpers[1][68]
    helpers[1][68] = good.replace(expected, "Warning Score")
    with pytest.raises(ValidationError, match="Warning Score"):
        check_summary_selectors(headers, helpers, expected)
    helpers[1][68] = good
    helpers[0][68] = "=B1"
    with pytest.raises(ValidationError, match="unrecognised|expected"):
        check_summary_selectors(headers, helpers, expected)
