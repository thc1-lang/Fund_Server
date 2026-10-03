"""The migration consumes updates without running any pipeline command."""

import json

from tools import configure_telegram_group as setup


def test_discovery_checkpoints_all_consumed_updates(monkeypatch, tmp_path):
    path = tmp_path / "offset.json"
    monkeypatch.setattr(setup, "OFFSET_PATH", path)
    monkeypatch.setattr(setup, "credential", lambda name: "existing-token")
    calls = []

    def api(method, **payload):
        calls.append((method, payload))
        if method == "getWebhookInfo":
            return {"url": ""}
        if payload.get("offset") == -1:
            return [{"update_id": 100, "message": {"text": "/run"}}]
        if payload.get("offset") == 101:
            return [
                {"update_id": 101, "message": {"chat": {"id": 100, "type": "private"}, "from": {"id": 1}}},
                {"update_id": 102, "message": {"chat": {"id": -100123, "title": "Fund", "type": "supergroup"}, "from": {"id": 1, "first_name": "Theo", "is_bot": False}}},
                {"update_id": 103, "message": {"chat": {"id": -100123, "title": "Fund", "type": "supergroup"}, "from": {"id": 9, "is_bot": True}}},
                {"update_id": 104, "message": {"chat": {"id": -100123, "title": "Fund", "type": "supergroup"}, "from": {"id": 2, "first_name": "Cofounder", "is_bot": False}}},
            ]
        return []

    monkeypatch.setattr(setup, "api", api)
    groups, offset = setup.discover(timeout_seconds=30)
    assert offset == 105
    assert json.loads(path.read_text()) == {"offset": 105}
    assert set(groups) == {-100123}
    assert set(groups[-100123]["users"]) == {1, 2}
    assert calls[1][1]["offset"] == -1
