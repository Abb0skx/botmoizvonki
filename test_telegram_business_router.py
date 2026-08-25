from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_business import router as business_router_module


class _Repo:
    def __init__(self):
        self.saved = []

    def save_update(self, payload, now, *, allowed_connection_id=None):
        assert allowed_connection_id == "connection"
        self.saved.append(payload)
        return len(self.saved) == 1


class _Service:
    def __init__(self):
        self.repo = _Repo()


def _client(monkeypatch, *, enabled=True):
    settings = replace(
        business_router_module.settings,
        enabled=enabled,
        webhook_secret="safe_secret",
        bot_token="123:token",
        bot_id="123",
        allowed_connection_id="connection",
    )
    service = _Service()
    monkeypatch.setattr(business_router_module, "settings", settings)
    monkeypatch.setattr(business_router_module, "service", service)
    app = FastAPI()
    app.include_router(business_router_module.router)
    return TestClient(app), service


def test_webhook_rejects_disabled_or_wrong_secret(monkeypatch):
    disabled, _ = _client(monkeypatch, enabled=False)
    assert disabled.post(
        "/webhooks/telegram-business",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "safe_secret"},
    ).status_code == 404

    client, service = _client(monkeypatch)
    assert client.post(
        "/webhooks/telegram-business",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    ).status_code == 403
    assert service.repo.saved == []


def test_webhook_only_persists_and_acknowledges_duplicates(monkeypatch):
    client, service = _client(monkeypatch)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "safe_secret"}
    payload = {
        "update_id": 1,
        "business_message": {
            "business_connection_id": "connection",
            "message_id": 10,
            "chat": {"id": 42, "type": "private"},
        },
    }
    first = client.post("/webhooks/telegram-business", json=payload, headers=headers)
    second = client.post("/webhooks/telegram-business", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json() == {"ok": True, "queued": True}
    assert second.json() == {"ok": True, "duplicate": True}
    assert service.repo.saved == [payload, payload]


def test_callback_query_is_durably_queued_without_inline_processing(monkeypatch):
    client, service = _client(monkeypatch)
    payload = {
        "update_id": 2,
        "callback_query": {
            "id": "callback-2",
            "from": {"id": 42, "language_code": "ru"},
            "data": "nr1:AbcdefghijklmnopQRSTUV",
            "message": {
                "business_connection_id": "connection",
                "message_id": 11,
                "chat": {"id": 42, "type": "private"},
            },
        },
    }
    response = client.post(
        "/webhooks/telegram-business",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "safe_secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "queued": True}
    assert service.repo.saved == [payload]


def test_documented_allowed_updates_include_only_business_events_and_callbacks():
    assert business_router_module.TELEGRAM_BUSINESS_ALLOWED_UPDATES == (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
        "callback_query",
    )


def test_executable_code_never_calls_read_business_message():
    package = Path(__file__).with_name("telegram_business")
    forbidden = "read" + "BusinessMessage"
    for source in package.glob("*.py"):
        assert forbidden not in source.read_text(encoding="utf-8"), source
