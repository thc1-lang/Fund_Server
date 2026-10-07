from summary_pipeline import alignment, archive_departed_ticker_folders, concentration, consolidated_history, managed_ticker_files, rows_from_sheet, safe_folder, secondary_reconciliation_error, statement_payload


def test_rows_find_nonstandard_third_row_header_and_numeric_cells():
    records = rows_from_sheet([["title"], ["note"], ["Rank", "Ticker", "Score"], ["1", " aapl ", "61.2"]], "Example")
    assert records == [{"Rank": 1.0, "Ticker": "aapl", "Score": 61.2, "_source_row": 4, "_source_sheet": "Example"}]


def test_safe_folder_preserves_ticker_but_blocks_path_characters():
    assert safe_folder("BRK/B") == "BRK_B"
    assert safe_folder("../odd ticker") == "odd_ticker"


def test_concentration_is_diagnostic_and_not_a_score():
    result = concentration({"Revenue Contribution": 6, "Trend Penalty Contribution": -2, "Noise": "x"})
    assert result["diagnostic_only"] is True
    assert result["top_1_positive_share"] == 1
    assert result["negative_contributor_count"] == 1


def test_alignment_is_explicitly_descriptive():
    result = alignment("Safe", {"Final Strategy Score": 70}, {"trend_direction_breadth": 0.4})
    assert result["classification"] == "contradiction"
    assert "never used for selection" in result["rule"]


def test_secondary_reconciliation_uses_published_short_formula():
    assert secondary_reconciliation_error("Short", {"Final Short Score": 62, "Raw Short Score": 70, "Trend Reliability": 0.6}) == 0


def test_secondary_reconciliation_retains_unrounded_precision_for_validation():
    assert secondary_reconciliation_error("Safe", {"Final Score": 60, "Raw Final Score": 65, "Trend Data Penalty": -5}) == 0


def test_departed_ticker_folder_is_archived_without_touching_unmanaged_data(tmp_path):
    active = tmp_path / "AAPL"
    active.mkdir()
    (active / "AAPL_AI.json").write_text("{}")
    departed = tmp_path / "OLD"
    departed.mkdir()
    (departed / "OLD_AI.json").write_text("{}")
    unrelated = tmp_path / "notes"
    unrelated.mkdir()
    (unrelated / "readme.txt").write_text("keep")

    retired = archive_departed_ticker_folders(tmp_path, {"AAPL"}, "2026-10-06T12:00:00Z")

    assert retired == ["OLD"]
    assert (tmp_path / "AAPL" / "AAPL_AI.json").is_file()
    assert (tmp_path / "retired" / "2026-10-06T12-00-00Z" / "OLD" / "OLD_AI.json").is_file()
    assert (unrelated / "readme.txt").is_file()


def test_statement_payload_contains_only_statement_line_items_and_explicit_nulls():
    history = [{
        "Fiscal Year": 2025, "Fiscal Period End": "2025-12-31", "Form": "10-K",
        "Accepted At": "2026-02-01T00:00:00Z", "Accession": "abc", "Revenue": 100,
        "Net Income": 12, "Operating Cash Flow": 20, "Free Cash Flow": 15,
        "Cash": 30, "Total Assets": 200, "Signal Score": 99, "_source_sheet": "Safe Data", "_source_row": 4,
    }]
    memberships = [{"category": "Safe", "history": history}, {"category": "High Growth Potential", "history": history}]

    payload = statement_payload({"ticker": "TEST"}, memberships, "income_statement", "2026-10-06T12:00:00Z")

    assert payload["document_type"] == "income_statement"
    assert len(payload["periods"]) == 1
    assert payload["periods"][0]["line_items"]["Revenue"] == 100
    assert payload["periods"][0]["line_items"]["Gross Profit"] is None
    assert "Signal Score" not in payload["periods"][0]["line_items"]
    assert payload["periods"][0]["source"]["categories"] == ["High Growth Potential", "Safe"]
    assert "fact_provenance_json" not in payload["periods"][0]["source"]


def test_consolidated_history_keeps_every_distinct_result_once_with_all_sources():
    base = {"Fiscal Year": 2025, "Revenue": 100, "Fact Provenance JSON": '{"revenue":{"concept":"Revenue"}}'}
    memberships = [
        {"category": "Safe", "history": [{**base, "_source_sheet": "Safe Data", "_source_row": 4}]},
        {"category": "High Growth Potential", "history": [{**base, "_source_sheet": "High Growth Potential Data", "_source_row": 8}]},
    ]

    history = consolidated_history(memberships)

    assert len(history) == 1
    assert history[0]["source_categories"] == ["High Growth Potential", "Safe"]
    assert len(history[0]["source_locations"]) == 2
    assert history[0]["record"]["Fact Provenance"]["revenue"]["concept"] == "Revenue"


def test_managed_ticker_files_reports_only_the_four_ai_contract_files(tmp_path):
    folder = tmp_path / "TEST"
    folder.mkdir()
    expected = {
        folder / "TEST_income_statement.json", folder / "TEST_cash_flow_statement.json",
        folder / "TEST_balance_sheet.json", folder / "TEST_detailed_summary.json",
    }
    for path in expected:
        path.write_text("{}")
    (folder / "notes.txt").write_text("keep")

    assert set(managed_ticker_files(folder)) == expected
