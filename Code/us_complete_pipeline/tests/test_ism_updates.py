from datetime import date
import pandas as pd
import pytest
import monthly_indicators as m


def frame():
    current = date.today().replace(day=1)
    releases = [m.add_months(current, -2), current]
    return pd.DataFrame(
        {
            "date": [d.isoformat() for d in releases],
            "report_month": [m.add_months(d, -1).isoformat() for d in releases],
            **{column: [51.0, 52.0] for column in m.ISM_VALUE_COLUMNS},
        }
    )


def test_missing_months_and_components_preserve_existing_history(monkeypatch):
    data = frame()
    data.loc[1, "services_pmi"] = float("nan")
    monkeypatch.setattr(m, "VALIDATION_MESSAGES", [])
    m.validate_ism_frame(data, expected_months=3)
    assert any(
        "Unavailable release month" in x["message"] for x in m.VALIDATION_MESSAGES
    )
    monkeypatch.setattr(m, "_ISM_CACHE", {3: data})
    monkeypatch.setattr(m, "_EXPLICIT_BLANK_OUTPUT_MONTHS", {})
    indicator = next(i for i in m.INDICATORS if i.source_column == "services_pmi")
    records = m.get_ism_indicator_observations(indicator, 3)
    incoming = m.sheet_rows_for_indicator(indicator.name, records)
    current = date.today().replace(day=1)
    assert current not in incoming
    # Exercise the real default history merge and write/readback path with a fake API.
    old = [
        ["Date", "Value"],
        *[
            [m.format_output_month(m.add_months(current, offset)), 49.0]
            for offset in [-3, -2, -1, 0]
        ],
    ]
    state = {"values": old}

    def request(method, url, token, body=None):
        if method == "PUT":
            state["values"] = body["values"]
        return {"values": state["values"]}

    monkeypatch.setattr(m, "google_sheets_request", request)
    monkeypatch.setattr(m, "FULL_HISTORY_MODE", True)
    m.sync_indicator_sheet_direct(
        "https://example.test", "fake", indicator.name, indicator.name, records
    )
    values = dict(state["values"][1:])
    assert values[m.format_output_month(current)] == 49.0
    assert values[m.format_output_month(m.add_months(current, -1))] == 49.0
    assert values[m.format_output_month(m.add_months(current, -2))] == 51.0
    assert len(values) == 4


@pytest.mark.parametrize("bad", [101, -1, "invalid"])
def test_known_invalid_values_still_fail(bad):
    data = frame().astype(object)
    data.loc[1, "manufacturing_pmi"] = bad
    with pytest.raises(RuntimeError, match="implausible|nonnumeric"):
        m.validate_ism_frame(data, expected_months=3)


def test_stale_reports_still_fail():
    data = frame()
    data["date"] = ["2020-01-01", "2020-02-01"]
    data["report_month"] = ["2019-12-01", "2020-01-01"]
    with pytest.raises(RuntimeError, match="stale"):
        m.validate_ism_frame(data, expected_months=3)


def test_missing_entire_component_is_no_update(monkeypatch):
    data = frame().drop(columns="services_pmi")
    m.validate_ism_frame(data, expected_months=3)
    monkeypatch.setattr(m, "_ISM_CACHE", {3: data})
    indicator = next(i for i in m.INDICATORS if i.source_column == "services_pmi")
    assert m.get_indicator_observations_safely("", indicator, 3) == []


def test_press_release_allowlist_is_scoped():
    for url in m.ISM_PRESS_RELEASES.values():
        m.validate_official_url(url, "ISM")
    with pytest.raises(m.OfficialSourceRedirectError):
        m.validate_official_url("https://www.prnewswire.com/unverified.html", "ISM")
