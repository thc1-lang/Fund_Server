import logging
import threading
import pytest
from google_sheets import request_progress
import run_relationships as main


def test_progress_stops_after_interrupt(caplog):
    emitted = threading.Event()

    class Signal(logging.Handler):
        def emit(self, record):
            emitted.set()

    logger = logging.getLogger("google_sheets")
    handler = Signal()
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO):
            with pytest.raises(KeyboardInterrupt):
                with request_progress("test", interval=0.01):
                    assert emitted.wait(2)
                    raise KeyboardInterrupt
        assert "still waiting for Google Sheets" in caplog.text
        assert not any(t.name == "sheets-progress" for t in threading.enumerate())
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize("writes", [False, True])
def test_clean_interrupt_records_possible_writes(tmp_path, monkeypatch, capsys, writes):
    monkeypatch.setattr("notifications.lifecycle.send_telegram", lambda *a, **kw: True)

    class FakeSheets:
        def __init__(self, *a, **kw):
            self.writes_attempted = writes

    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "credentials_path", lambda *a: "unused")
    monkeypatch.setattr("source_freshness.validate_macro_summary", lambda *a: None)
    monkeypatch.setattr(main, "Sheets", FakeSheets)
    monkeypatch.setattr(main, "load", interrupt)
    assert main.main(["--correlation"]) == 130
    import json

    report = json.loads(
        next((tmp_path / "work").glob("*/interrupted.json")).read_text()
    )
    assert report["writes_attempted"] == writes
    assert "Traceback" not in capsys.readouterr().err
