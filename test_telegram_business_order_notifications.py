from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram_business.repository import BusinessRepository
from telegram_business.service import BusinessService


NOW = datetime(2026, 8, 28, 21, 0, tzinfo=ZoneInfo("Asia/Tashkent"))


class API:
    def __init__(self):
        self.messages = []

    def send_chat_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return {"ok": True, "result": {"message_id": 9001}}


def request_row():
    return {
        "request_id": "req-1",
        "chat_id": "123456",
        "session_id": "session-1",
        "status": "submitted",
        "exact_model": "iPhone 16 Pro Max",
        "option_kind": "memory",
        "option_value": "256 GB",
        "color": "Black",
        "color_any": 0,
        "database_price": "14500000",
        "fulfillment_method": "delivery",
        "phone": "+998901234567",
        "contact_method": "phone",
        "location_url": "https://maps.google.com/?q=41.3,69.2",
        "address": None,
        "preferred_time": "11:00",
    }


def test_completed_request_is_scheduled_once(tmp_path: Path):
    service = BusinessService.__new__(BusinessService)
    service.repo = BusinessRepository(tmp_path / "business.db")
    service.settings = SimpleNamespace(orders_chat_id="-1004307725887")

    service.schedule_order_notification(request_row(), NOW)

    actions = service.repo.due_actions(NOW)
    assert len(actions) == 1
    assert actions[0]["dedupe_key"] == "request-notify:req-1"
    assert actions[0]["action_type"] == "request_notify"


def test_notification_contains_collected_order_and_is_not_duplicated(tmp_path: Path):
    service = BusinessService.__new__(BusinessService)
    service.repo = BusinessRepository(tmp_path / "business.db")
    service.api = API()
    service.settings = SimpleNamespace(orders_chat_id="-1004307725887")
    service.repo.business_request = lambda _request_id: request_row()
    service.repo.client = lambda _chat_id: {
        "first_name": "Abbos",
        "username": "abbos_test",
    }

    service._send_order_notification("req-1", "-1004307725887", NOW)
    service._send_order_notification("req-1", "-1004307725887", NOW)

    assert len(service.api.messages) == 1
    destination, text = service.api.messages[0]
    assert destination == "-1004307725887"
    assert "Модель: iPhone 16 Pro Max" in text
    assert "Память: 256 GB" in text
    assert "Цена в базе: 14 500 000 so'm" in text
    assert "Получение: доставка" in text
    assert "Телефон: +998901234567" in text
    assert "Локация: https://maps.google.com/?q=41.3,69.2" in text
