import json
import tempfile
import time as system_time
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook
from unittest.mock import patch

from telegram_business.config import BusinessSettings
from telegram_business.intents import (
    classify,
    extract_preferred_time,
    extract_text_location,
    is_outside_tashkent,
)
from telegram_business.language import detect_language
from telegram_business.migrations import connect
from telegram_business.products import (
    ExistingGoogleProductRepository,
    ProductMatch,
    ProductVariant,
    extract_product_query,
    format_ambiguous_result,
    format_result,
    load_model_urls,
    load_product_urls,
    model_link_keyboard,
    normalize_model,
    safe_product_url,
)
from telegram_business.service import BusinessService


TZ = ZoneInfo("Asia/Tashkent")


def make_settings(path: Path, **changes) -> BusinessSettings:
    base = BusinessSettings(
        False, "999:token", "secret", "connection", "", "Asia/Tashkent",
        time(20), time(9, 30), time(10), time(20), 300, 3, 120, 720, 4, 8,
        path, "sheet", 60, 300, "existing_google_bot_prices", "", 1440,
    )
    return replace(base, **changes)


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


class FakeAPI:
    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    def send_message(self, connection_id, chat_id, text, **options):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append({"connection_id": connection_id, "chat_id": chat_id, "text": text, **options})
        return {"result": {"message_id": 1000 + len(self.sent)}}

    def get_business_connection(self, connection_id):
        return {
            "id": connection_id, "user": {"id": 100}, "is_enabled": True,
            "rights": {"can_reply": True},
        }


class FakeProducts:
    def __init__(self, unavailable: bool = False):
        self.queries = []
        self.unavailable = unavailable

    def search(self, query, memory=None, color=None):
        self.queries.append((query, memory, color))
        if self.unavailable:
            raise RuntimeError("catalog unavailable")
        if "iphone 16 pro max" in normalize_model(query):
            url = "https://t.me/Texnikach_Phone/1293"
            variant = ProductVariant("iPhone 16 Pro Max", "256 GB", "Black", Decimal("14500000"), 1, url)
            return ProductMatch("found", (variant.model,), (variant,), ((variant.model, url),))
        return ProductMatch("not_found")


class FakeAmbiguousProducts:
    def __init__(self):
        self.queries = []

    def search(self, query, memory=None, color=None):
        self.queries.append((query, memory, color))
        models = ("Samsung Galaxy S25", "Samsung Galaxy S25 Plus")
        urls = (
            (models[0], "https://t.me/Texnikach_Phone/1500"),
            (models[1], "https://t.me/Texnikach_Phone/1501"),
        )
        return ProductMatch("ambiguous", models, (), urls)


class FakeRecognizedProducts(FakeProducts):
    def recognizes_query(self, query):
        return normalize_model(query) == "dualsense wireless controller"

    def search(self, query, memory=None, color=None):
        self.queries.append((query, memory, color))
        variant = ProductVariant(
            "DualSense Wireless Controller", "", "White", Decimal("900000"),
            2, "https://t.me/Texnikach_Game/100",
        )
        return ProductMatch(
            "found", (variant.model,), (variant,), ((variant.model, variant.url),),
        )


class Harness:
    def __init__(self, root: Path, now: datetime, *, products=None, api=None, **setting_changes):
        self.clock = Clock(now)
        self.api = api or FakeAPI()
        self.products = products or FakeProducts()
        self.service = BusinessService(
            make_settings(root / "business.db", **setting_changes),
            clock=self.clock, api=self.api, products=self.products,
        )
        self.service.repo.upsert_connection(
            {"id": "connection", "user": {"id": 100}, "is_enabled": True, "rights": {"can_reply": True}}, now,
        )
        self.update_id = 0

    def incoming(
        self, event_at: datetime, text=None, *, chat_id=200, location=None,
        language_code="ru", message_fields=None,
    ):
        self.update_id += 1
        message = {
            "business_connection_id": "connection", "message_id": self.update_id,
            "date": int(event_at.timestamp()), "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "language_code": language_code},
        }
        if text is not None:
            message["text"] = text
        if location is not None:
            message["location"] = location
        if message_fields:
            message.update(message_fields)
        update = {"update_id": self.update_id, "business_message": message}
        assert self.service.repo.save_update(update, self.clock())
        self.service.process_update(update)
        return self.service.repo.session(str(chat_id), event_at, time(20), time(9, 30))

    def run_debounce(self, seconds=3):
        self.clock.value += timedelta(seconds=seconds)
        actions = [row for row in self.service.repo.due_actions(self.clock()) if row["action_type"] == "debounce"]
        assert actions
        action = actions[-1]
        assert self.service.repo.claim_action(action["action_id"], self.clock(), expected_generation=action["generation"])
        self.service.execute(action)


def bot_templates(harness: Harness, chat_id=200) -> list[str]:
    with connect(harness.service.repo.path) as db:
        return [
            row["template_code"]
            for row in db.execute(
                """SELECT template_code FROM business_messages
                   WHERE chat_id=? AND sender_type='business_bot' ORDER BY id""",
                (str(chat_id),),
            ).fetchall()
        ]


def test_daytime_sends_only_credit_and_silences_order_handoff(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 23, 12, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "беру, оформите", chat_id=201)
    harness.run_debounce()
    harness.incoming(harness.clock(), "у меня жалоба и брак", chat_id=202)
    harness.run_debounce()
    assert harness.api.sent == []

    harness.incoming(harness.clock(), "можно в рассрочку?", chat_id=203)
    harness.run_debounce()
    harness.clock.value += timedelta(seconds=10)
    credit = [row for row in harness.service.repo.due_actions(harness.clock()) if row["action_type"] == "credit"]
    assert len(credit) == 1
    harness.service.execute(credit[0])
    assert len(harness.api.sent) == 1
    assert "нет кредита" in harness.api.sent[0]["text"]


def test_day_credit_is_not_cancelled_by_simultaneous_handoff(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 23, 12, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(), "У меня жалоба, позовите менеджера: есть кредит?",
    )
    harness.run_debounce()
    assert harness.api.sent == []
    harness.clock.value += timedelta(seconds=10)
    actions = [
        row for row in harness.service.repo.due_actions(harness.clock())
        if row["action_type"] == "credit"
    ]
    assert len(actions) == 1
    harness.service.execute(actions[0])
    assert bot_templates(harness) == ["credit"]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["automation_handoff"] == 1
    assert saved["priority"] == 1


@pytest.mark.parametrize(
    "message,required_fragments",
    [
        ("Пришлите каталог", ("https://texnikach.uz/go",)),
        ("Какие условия доставки по Ташкенту?", ("бесплат", "2–3")),
        ("Можно приехать на самовывоз?", ("самовывоз", "подтвержд")),
        ("Как проверить наличие?", ("налич", "менеджер")),
    ],
)
def test_standalone_safe_intents_answer_at_night_without_product_search(
    tmp_path, message, required_fragments,
):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), message)
    harness.run_debounce()
    combined = "\n".join(item["text"] for item in harness.api.sent).casefold()
    assert all(fragment.casefold() in combined for fragment in required_fragments)
    assert harness.products.queries == []


@pytest.mark.parametrize(
    "message",
    [
        "Пришлите каталог",
        "Какие условия доставки по Ташкенту?",
        "Можно приехать на самовывоз?",
        "Как проверить наличие?",
    ],
)
def test_standalone_safe_intents_are_silent_during_day(tmp_path, message):
    harness = Harness(tmp_path, datetime(2026, 8, 23, 12, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), message)
    harness.run_debounce()
    assert harness.api.sent == []
    assert harness.products.queries == []


@pytest.mark.parametrize(
    "event_at,execution_at,should_reply",
    [
        (datetime(2026, 8, 22, 19, 59, 59, tzinfo=TZ), datetime(2026, 8, 22, 20, 0, 5, tzinfo=TZ), False),
        (datetime(2026, 8, 22, 20, 0, 0, tzinfo=TZ), datetime(2026, 8, 22, 20, 0, 5, tzinfo=TZ), True),
        (datetime(2026, 8, 23, 9, 29, 59, tzinfo=TZ), datetime(2026, 8, 23, 9, 30, 5, tzinfo=TZ), True),
        (datetime(2026, 8, 23, 9, 30, 0, tzinfo=TZ), datetime(2026, 8, 23, 9, 30, 5, tzinfo=TZ), False),
    ],
)
def test_schedule_uses_message_event_time(tmp_path, event_at, execution_at, should_reply):
    harness = Harness(tmp_path, execution_at - timedelta(seconds=3))
    harness.incoming(event_at, "iPhone 16 Pro Max")
    harness.run_debounce()
    assert bool(harness.api.sent) is should_reply
    assert bool(harness.products.queries) is should_reply


def test_message_at_0931_is_saved_but_never_auto_answered(tmp_path):
    event_at = datetime(2026, 8, 23, 9, 31, tzinfo=TZ)
    harness = Harness(tmp_path, event_at)
    session = harness.incoming(event_at, "iPhone 16 Pro Max")
    harness.run_debounce()
    assert harness.api.sent == []
    assert harness.products.queries == []
    assert harness.service.repo.session_by_id(session["session_id"])["status"] == "waiting_manager"


def test_final_is_scheduled_and_sent_at_0930_cutoff(tmp_path):
    event_at = datetime(2026, 8, 23, 9, 29, 10, tzinfo=TZ)
    cutoff = datetime(2026, 8, 23, 9, 30, tzinfo=TZ)
    harness = Harness(tmp_path, event_at)
    session = harness.incoming(event_at, "iPhone 16 Pro Max")
    harness.run_debounce()
    with connect(harness.service.repo.path) as db:
        final = db.execute(
            "SELECT * FROM scheduled_actions WHERE dedupe_key=?",
            (f"final:{session['session_id']}",),
        ).fetchone()
    assert datetime.fromisoformat(final["execute_at"]) == cutoff

    harness.clock.value = cutoff
    final = [
        row for row in harness.service.repo.due_actions(cutoff)
        if row["action_type"] == "final"
    ][0]
    assert harness.service.repo.claim_action(
        final["action_id"], cutoff, expected_generation=final["generation"],
    )
    harness.service.execute(final)
    assert harness.service.repo.session_by_id(session["session_id"])["final_sent"] == 1
    with connect(harness.service.repo.path) as db:
        sent = db.execute(
            """SELECT created_at FROM business_messages
               WHERE session_id=? AND template_code='final'""",
            (session["session_id"],),
        ).fetchone()
    assert datetime.fromisoformat(sent["created_at"]) == cutoff


def test_credit_only_does_not_trigger_greeting_or_search(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "можно ли купить в рассрочку без первоначального взноса?")
    harness.run_debounce()
    assert len(harness.api.sent) == 1
    assert "нет кредита" in harness.api.sent[0]["text"]
    assert harness.products.queries == []


def test_credit_order_words_are_removed_from_model_query(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "iPhone 16 Pro Max кредит беру оформите")
    harness.run_debounce()
    assert harness.products.queries[0][0] == "iphone 16 pro max"
    texts = [item["text"] for item in harness.api.sent]
    assert any("нет кредита" in text for text in texts)
    assert any("не могу оформить" in text for text in texts)
    assert any("so'm" in text for text in texts)


def test_ordinary_night_conversation_is_not_a_failed_product_search(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "Как дела?")
    harness.run_debounce()
    assert harness.products.queries == []
    assert bot_templates(harness) == ["greeting_no_model"]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["failed_searches"] == 0
    assert saved["model_query"] is None


def test_compound_preferred_time_is_removed_from_model_query(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(), "iPhone 16 Pro Max завтра в 11:30",
    )
    harness.run_debounce()
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["preferred_time"] == "завтра в 11:30"


def test_memory_and_color_sent_before_model_are_reused(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "256 black")
    harness.run_debounce()
    harness.clock.value += timedelta(seconds=10)
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert harness.products.queries == [("iphone 16 pro max", "256 GB", "black")]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["memory"] == "256 GB"
    assert saved["color"] == "black"


def test_bare_filter_followup_refines_model_without_duplicate_result(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(seconds=20)
    harness.incoming(harness.clock(), "256 black")
    harness.run_debounce()
    assert harness.products.queries[-1] == ("iPhone 16 Pro Max", "256 GB", "black")
    assert bot_templates(harness).count("product_result") == 1


def test_telegram_location_before_model_is_saved_and_acknowledged(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(), location={"latitude": 41.311081, "longitude": 69.240562}, language_code="ru",
    )
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["location_received"] == 1
    assert saved["location_url"] == "https://www.google.com/maps?q=41.311081,69.240562"
    assert len(harness.api.sent) == 1
    assert "Локацию получили" in harness.api.sent[0]["text"]
    assert harness.products.queries == []


def test_stored_location_does_not_block_later_approved_model_name(tmp_path):
    products = FakeRecognizedProducts()
    harness = Harness(
        tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ), products=products,
    )
    harness.incoming(harness.clock(), "Адрес: Юнусабад")
    harness.run_debounce()
    harness.clock.value += timedelta(minutes=1)
    harness.incoming(harness.clock(), "DualSense Wireless Controller")
    harness.run_debounce()
    assert products.queries == [("dualsense wireless controller", None, None)]
    assert "product_result" in bot_templates(harness)


def test_stored_location_does_not_finalize_on_later_non_location_details(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "Адрес: Юнусабад")
    harness.run_debounce()
    harness.clock.value += timedelta(minutes=1)
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(minutes=1)
    detail_at = harness.clock()
    harness.incoming(harness.clock(), "256 black")
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["final_sent"] == 0
    with connect(harness.service.repo.path) as db:
        action = db.execute(
            "SELECT execute_at,status FROM scheduled_actions WHERE dedupe_key=?",
            (f"final:{session['session_id']}",),
        ).fetchone()
    assert action["status"] == "pending"
    assert datetime.fromisoformat(action["execute_at"]) == detail_at + timedelta(seconds=300)


def test_location_after_price_sends_final_immediately(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert harness.service.repo.session_by_id(session["session_id"])["price_sent"] == 1

    harness.clock.value += timedelta(seconds=30)
    harness.incoming(harness.clock(), location={"latitude": 41.3, "longitude": 69.2})
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["final_sent"] == 1
    assert "Цена и наличие" in harness.api.sent[-1]["text"]
    with connect(harness.service.repo.path) as db:
        status = db.execute("SELECT status FROM scheduled_actions WHERE dedupe_key=?", (f"final:{session['session_id']}",)).fetchone()[0]
    assert status == "cancelled"


def test_location_after_already_sent_final_never_asks_for_model(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value = datetime(2026, 8, 22, 21, 5, tzinfo=TZ)
    final = [
        row for row in harness.service.repo.due_actions(harness.clock())
        if row["action_type"] == "final"
    ][0]
    assert harness.service.repo.claim_action(
        final["action_id"], harness.clock(), expected_generation=final["generation"],
    )
    harness.service.execute(final)
    sent_before_location = len(harness.api.sent)

    harness.clock.value += timedelta(seconds=10)
    harness.incoming(
        harness.clock(), location={"latitude": 41.311081, "longitude": 69.240562},
    )
    harness.run_debounce()
    new_text = "\n".join(item["text"] for item in harness.api.sent[sent_before_location:]).casefold()
    assert not ("напишите" in new_text and "модел" in new_text)
    assert bot_templates(harness).count("final") == 1
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["location_received"] == 1


def test_details_after_final_receive_rate_limited_ack(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(minutes=5)
    final = [row for row in harness.service.repo.due_actions(harness.clock()) if row["action_type"] == "final"][0]
    assert harness.service.repo.claim_action(
        final["action_id"], harness.clock(), expected_generation=final["generation"],
    )
    harness.service.execute(final)
    harness.clock.value += timedelta(seconds=10)
    harness.incoming(harness.clock(), "удобно завтра в 11:30")
    harness.run_debounce()
    assert bot_templates(harness)[-1] == "data_added"
    assert harness.service.repo.session_by_id(session["session_id"])["preferred_time"] == "завтра в 11:30"


@pytest.mark.parametrize(
    "message,expected_flag",
    [
        ("У меня жалоба, адрес: улица Амира Темура 10", "handoff"),
        ("Беру, доставьте сюда https://www.google.com/maps?q=41.31,69.24", "order"),
    ],
)
def test_location_after_price_does_not_hide_critical_intents(tmp_path, message, expected_flag):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(seconds=30)
    harness.incoming(harness.clock(), message)
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["final_sent"] == 1
    if expected_flag == "handoff":
        assert saved["priority"] == 1
        assert saved["automation_handoff"] == 1
        assert "human_handoff" in bot_templates(harness)
    else:
        assert saved["order_intent"] == 1
        assert "order_request" in bot_templates(harness)


def test_credit_with_location_after_final_is_not_swallowed(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 20, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(minutes=5)
    final = [row for row in harness.service.repo.due_actions(harness.clock()) if row["action_type"] == "final"][0]
    assert harness.service.repo.claim_action(
        final["action_id"], harness.clock(), expected_generation=final["generation"],
    )
    harness.service.execute(final)
    harness.clock.value += timedelta(seconds=10)
    harness.incoming(harness.clock(), "кредит? Адрес: Чиланзар")
    harness.run_debounce()
    assert bot_templates(harness).count("credit") == 1
    assert bot_templates(harness).count("final") == 1


def test_new_details_after_price_move_final_and_ack_once(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    harness.clock.value += timedelta(seconds=60)
    detail_time = harness.clock()
    harness.incoming(detail_time, "256 GB черный, удобно в 11:30")
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["memory"] == "256 GB"
    assert saved["color"] == "black"
    assert "11:30" in saved["preferred_time"]
    assert harness.api.sent[-1]["text"] == "Данные добавлены."
    with connect(harness.service.repo.path) as db:
        action = db.execute("SELECT execute_at,status FROM scheduled_actions WHERE dedupe_key=?", (f"final:{session['session_id']}",)).fetchone()
    assert action["status"] == "pending"
    assert datetime.fromisoformat(action["execute_at"]) == detail_time + timedelta(seconds=300)


def test_new_message_moves_final_before_debounce_can_run(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    with connect(harness.service.repo.path) as db:
        previous = db.execute(
            "SELECT execute_at FROM scheduled_actions WHERE dedupe_key=?",
            (f"final:{session['session_id']}",),
        ).fetchone()["execute_at"]

    harness.clock.value += timedelta(seconds=60)
    detail_time = harness.clock()
    harness.incoming(detail_time, "256 GB черный")
    # Do not run debounce: persisting the new inbound message itself must move
    # the durable final action, so the old action cannot race the debounce.
    with connect(harness.service.repo.path) as db:
        moved = db.execute(
            "SELECT execute_at,status FROM scheduled_actions WHERE dedupe_key=?",
            (f"final:{session['session_id']}",),
        ).fetchone()
    assert moved["status"] == "pending"
    assert datetime.fromisoformat(moved["execute_at"]) == detail_time + timedelta(seconds=300)
    assert moved["execute_at"] > previous


def test_availability_question_with_model_still_gets_night_price(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "Есть ли в наличии iPhone 16 Pro Max 256?")
    harness.run_debounce()
    assert harness.products.queries == [("iphone 16 pro max", "256 GB", None)]
    assert any("so'm" in item["text"] for item in harness.api.sent)


def test_processed_message_metadata_is_written_to_sqlite_and_sheets_outbox(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "Какая цена iPhone 16 Pro Max?")
    harness.run_debounce()
    with connect(harness.service.repo.path) as db:
        message = db.execute(
            "SELECT language,intent,model_query FROM business_messages WHERE sender_type='client'"
        ).fetchone()
        payload = json.loads(db.execute(
            "SELECT payload FROM sheets_outbox WHERE entity_type='message' ORDER BY id LIMIT 1"
        ).fetchone()[0])
    assert message["language"] == "ru"
    assert "product_search" in message["intent"].split(";")
    assert message["model_query"] == "iphone 16 pro max"
    assert payload["processed_status"] == "processed"
    assert payload["language"] == "ru"
    assert "product_search" in payload["intent"]


def test_handoff_stops_future_session_automation(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "У меня жалоба, позовите менеджера")
    harness.run_debounce()
    assert len(harness.api.sent) == 1


def test_existing_order_sets_permanent_client_pause(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(), "Мой заказ уже оформлен, хочу изменить заказ",
    )
    harness.run_debounce()
    client = harness.service.repo.client("200")
    assert client["bot_paused"] == 1
    assert client["pause_reason"] == "active_order"
    assert "human_handoff" in bot_templates(harness)
    assert harness.service.repo.session_by_id(session["session_id"])["automation_handoff"] == 1
    harness.clock.value += timedelta(seconds=20)
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert len(harness.api.sent) == 1


def test_delivery_outside_tashkent_is_handed_to_manager(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "Нужна доставка в Самарканд")
    harness.run_debounce()
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["priority"] == 1
    assert saved["needs_manager_reply"] == 1
    assert saved["automation_handoff"] == 1
    assert "передано менеджеру" in "\n".join(item["text"] for item in harness.api.sent).casefold()
    assert harness.products.queries == []


@pytest.mark.parametrize(
    "city", ["Чирчик", "Ургенч", "Коканд", "Gulistan", "Angren", "Olmaliq"],
)
def test_named_cities_outside_tashkent_are_routed_to_manager(city):
    assert is_outside_tashkent(f"Доставка в {city}")


def test_address_before_model_keeps_both_location_and_product(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(), "Адрес: Чиланзар 10, модель iPhone 16 Pro Max",
    )
    harness.run_debounce()
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["location_received"] == 1
    assert saved["matched_model"] == "iPhone 16 Pro Max"


def test_identical_price_result_not_repeated_for_ten_minutes(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    first_count = len(harness.api.sent)
    harness.clock.value += timedelta(minutes=1)
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert len(harness.api.sent) == first_count


def test_identical_ambiguous_result_is_not_repeated_for_ten_minutes(tmp_path):
    products = FakeAmbiguousProducts()
    harness = Harness(
        tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ), products=products,
    )
    harness.incoming(harness.clock(), "Samsung S25")
    harness.run_debounce()
    assert bot_templates(harness).count("ambiguous") == 1

    harness.clock.value += timedelta(minutes=1)
    harness.incoming(harness.clock(), "Samsung S25")
    harness.run_debounce()
    assert bot_templates(harness).count("ambiguous") == 1


def test_product_fingerprint_includes_requested_filter_mismatch():
    variant = ProductVariant("Phone X", "256 GB", "Black", Decimal("1000000"))
    memory = ProductMatch(
        "found", ("Phone X",), (variant,), (),
        requested_memory="1 TB", unmatched_filters=("memory",),
    )
    color = ProductMatch(
        "found", ("Phone X",), (variant,), (),
        requested_color="White", unmatched_filters=("color",),
    )
    assert BusinessService._product_fingerprint(memory) != BusinessService._product_fingerprint(color)


def test_unknown_model_uses_short_greeting_then_two_attempts_without_loop(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "UnknownPhone ZX999")
    harness.run_debounce()
    assert bot_templates(harness) == ["greeting_model", "not_found_1"]

    harness.clock.value += timedelta(seconds=10)
    harness.incoming(harness.clock(), "UnknownPhone ZX999 full")
    harness.run_debounce()
    assert bot_templates(harness) == ["greeting_model", "not_found_1", "not_found_2"]

    searches_before = len(harness.products.queries)
    sent_before = len(harness.api.sent)
    harness.clock.value += timedelta(seconds=10)
    harness.incoming(harness.clock(), "UnknownPhone ZX999 again")
    harness.run_debounce()
    assert len(harness.products.queries) == searches_before
    assert len(harness.api.sent) == sent_before
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["failed_searches"] == 2
    assert saved["search_disabled"] == 1


def test_ambiguous_and_unavailable_queries_are_persisted_for_manager(tmp_path):
    ambiguous = Harness(
        tmp_path / "ambiguous", datetime(2026, 8, 22, 21, 0, tzinfo=TZ),
        products=FakeAmbiguousProducts(),
    )
    session = ambiguous.incoming(ambiguous.clock(), "Samsung S25")
    ambiguous.run_debounce()
    assert ambiguous.service.repo.session_by_id(session["session_id"])["model_query"] == "samsung s25"

    unavailable = Harness(
        tmp_path / "unavailable", datetime(2026, 8, 22, 21, 0, tzinfo=TZ),
        products=FakeProducts(unavailable=True),
    )
    session = unavailable.incoming(unavailable.clock(), "iPhone 16 Pro Max")
    unavailable.run_debounce()
    assert unavailable.service.repo.session_by_id(session["session_id"])["model_query"] == "iphone 16 pro max"


def test_day_model_query_is_saved_while_bot_stays_silent(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 23, 12, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert harness.api.sent == []
    assert harness.service.repo.session_by_id(session["session_id"])["model_query"] == "iphone 16 pro max"


def test_failed_send_does_not_set_greeting_or_price_flags(tmp_path):
    harness = Harness(
        tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ), api=FakeAPI(fail=True),
    )
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.clock.value += timedelta(seconds=3)
    action = [row for row in harness.service.repo.due_actions(harness.clock()) if row["action_type"] == "debounce"][0]
    with pytest.raises(RuntimeError, match="send failed"):
        harness.service.execute(action)
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["greeting_sent"] == 0
    assert saved["price_sent"] == 0


def test_unavailable_product_source_sends_fallback_and_records_error(tmp_path):
    harness = Harness(
        tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ), products=FakeProducts(unavailable=True),
    )
    session = harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.run_debounce()
    assert "не удалось получить актуальные цены" in harness.api.sent[-1]["text"]
    saved = harness.service.repo.session_by_id(session["session_id"])
    assert saved["automation_handoff"] == 1
    with connect(harness.service.repo.path) as db:
        assert db.execute("SELECT count(*) FROM business_errors WHERE operation='product_search'").fetchone()[0] == 1


def test_language_context_and_bilingual_fallback():
    assert detect_language("iPhone 16 Pro Max", history="Narxi qancha?")[0] == "uz"
    assert detect_language("Narxi qancha?", saved="ru")[0] == "uz"
    assert detect_language("iPhone 16 Pro Max")[0] == "bi"
    assert detect_language("Қанча, нархини айтинг")[0] == "uz"


def test_query_normalization_memory_color_plus_and_cyrillic_generation():
    query, memory, color = extract_product_query("самсунг с25+ ультра 8/256 черный кредит")
    assert query == "samsung s25 plus ultra"
    assert memory == "256 GB"
    assert color == "black"
    assert normalize_model("S25+") == "s25 plus"

    query, memory, color = extract_product_query("S25 ultra 256 қора нархи қанча")
    assert query == "s25 ultra"
    assert memory == "256 GB"
    assert color == "black"

    query, memory, color = extract_product_query("самсунг с25 ультра мавжудми доставка")
    assert query == "samsung s25 ultra"
    assert memory is None
    assert color is None


def test_safe_family_search_typo_and_clickable_bilingual_output():
    repo = ExistingGoogleProductRepository()
    url = "https://t.me/Texnikach_Phone/1553"
    repo._variants = [
        ProductVariant("Samsung Galaxy S25 Plus", "256 GB", "Black", Decimal("10000000"), 1, url),
        ProductVariant("Samsung Galaxy S25 Ultra", "256 GB", "Black", Decimal("11000000"), 2, url),
    ]
    repo._loaded = system_time.time()
    assert repo.search("самсунг с25+").status == "found"
    assert repo.search("samsng s25 plus").status == "found"
    assert repo.search("samnsung s25 ulrta").models == ("Samsung Galaxy S25 Ultra",)
    match = repo.search("Samsung S25 Plus")
    text = format_result(match, "bi")
    assert "Samsung Galaxy S25 Plus" in text
    assert '<a href="https://t.me/Texnikach_Phone/1553">' in text
    assert len(text) <= 4096

    ambiguous = ProductMatch(
        "ambiguous", ("Phone A", "Phone B"), (),
        (("Phone A", "https://t.me/a/1"), ("Phone B", "https://t.me/b/2")),
    )
    output = format_ambiguous_result(ambiguous, "bi")
    assert output.count("<a href=") == 2
    assert len(output) <= 4096


def test_location_and_time_extractors_are_conservative():
    assert extract_text_location("41.311081, 69.240562") == "https://www.google.com/maps?q=41.311081,69.240562"
    assert extract_text_location("улица Амира Темура, дом 12").startswith("https://www.google.com/maps/search/")
    assert extract_text_location("iPhone 16 Pro Max") is None
    assert extract_preferred_time("Мне удобно в 11:30") == "в 11:30"
    assert extract_preferred_time("Удобно завтра в 11:30") == "завтра в 11:30"
    assert extract_text_location("Адрес: Юнусабад").startswith("https://www.google.com/maps/search/")
    assert extract_text_location("manzil Yunusobod").startswith("https://www.google.com/maps/search/")


def test_map_links_accept_only_known_google_and_yandex_hosts():
    assert extract_text_location("https://maps.app.goo.gl/AbCdEf123") == "https://maps.app.goo.gl/AbCdEf123"
    assert extract_text_location("https://goo.gl/maps/AbCdEf123") == "https://goo.gl/maps/AbCdEf123"
    assert extract_text_location("https://www.google.com/maps/place/Tashkent") == "https://www.google.com/maps/place/Tashkent"
    assert extract_text_location("https://yandex.uz/maps/10335/tashkent/") == "https://yandex.uz/maps/10335/tashkent/"
    assert extract_text_location("https://google.evil/maps/place/Tashkent") is None
    assert extract_text_location("https://yandex.evil/maps/10335/tashkent") is None
    assert extract_text_location("https://user@google.com/maps/place/Tashkent") is None
    assert extract_text_location("http://google.com/maps/place/Tashkent") is None
    assert extract_text_location("https://example.test/do-not-open") is None


def test_builtin_intents_cover_uzbek_latin_and_cyrillic_safety_fallbacks():
    assert "credit" in classify("kredit bormi?")
    assert "credit" in classify("муддатли тўлов борми?")
    assert "credit" not in classify("кредит керак эмас, тўлиқ тўлайман")
    assert "credit" not in classify("кредитсиз тўлиқ тўлов қиламан")
    assert "order_request" in classify("буюртма қиламан, юборинг")
    assert "warranty" in classify("кафолат керак")
    assert "technical" in classify("Какая камера у iPhone 16?")


@pytest.mark.parametrize(
    "message",
    [
        "измените адрес доставки в заказе",
        "Где заказ №123?",
        "Курьер уже едет?",
        "Заказ оформлял вчера",
        "Отмените заказ",
        "buyurtma manzilini o'zgartiring",
        "buyurtmam qayerda?",
        "kuryer yo'lda",
        "kecha buyurtma berganman",
        "buyurtmani bekor qiling",
        "буюртма манзилини ўзгартиринг",
        "буюртмам қаерда?",
        "курьер йўлда",
        "кеча буюртма берганман",
        "буюртмани бекор қилинг",
    ],
)
def test_active_order_and_current_delivery_forms_require_human(message):
    detected = classify(message)
    assert "active_order" in detected
    assert "order_request" not in detected


@pytest.mark.parametrize(
    "message",
    [
        "измените адрес доставки в заказе",
        "buyurtmam qayerda?",
        "буюртмани бекор қилинг",
    ],
)
def test_active_order_forms_are_handed_off_end_to_end(tmp_path, message):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), message)
    harness.run_debounce()

    saved = harness.service.repo.session_by_id(session["session_id"])
    assert bot_templates(harness) == ["human_handoff"]
    assert saved["priority"] == 1
    assert saved["handoff_reason"] == "active_order"
    assert saved["order_intent"] == 0


def test_redacted_payment_data_is_handed_off_without_exposure(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(
        harness.clock(),
        "Карта 8600 1234 5678 9012, срок 12/28, CVV 123",
    )
    harness.run_debounce()

    saved = harness.service.repo.session_by_id(session["session_id"])
    messages = harness.service.repo.session_messages(session["session_id"])
    assert bot_templates(harness) == ["human_handoff"]
    assert saved["priority"] == 1
    assert saved["handoff_reason"] == "payment"
    assert "[PAYMENT_DATA_REDACTED]" in messages[0]["text"]
    assert "8600" not in messages[0]["text"]


@pytest.mark.parametrize(
    "message", ["кредитка", "аккредитив", "кредитор", "вопрос по кредитной карте"],
)
def test_credit_word_boundaries_avoid_card_and_unrelated_false_positives(message):
    assert "credit" not in classify(message)


@pytest.mark.parametrize(
    "message,code", [
        ("есть кредитка?", "credit"),
        ("аккредитив", "credit"),
        ("кредитор", "credit"),
        ("вопрос по кредитной карте", "credit"),
        ("я не беру iPhone 16", "order_request"),
        ("не доставьте пока", "order_request"),
    ],
)
def test_runtime_sheet_substrings_cannot_bypass_safety_veto(tmp_path, message, code):
    from telegram_business.sheets import INTENT_SEED, IntentOverride

    class SeedIntents:
        def intents_cached(self):
            return tuple(
                IntentOverride(
                    row[0], row[1], row[2], row[3],
                    tuple(row[4].split(";")), tuple(row[5].split(";")),
                    tuple(row[6].split(";")) if row[6] else (),
                    row[7], row[8], row[9],
                )
                for row in INTENT_SEED
            )

    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.service.sheets = SeedIntents()
    detected = harness.service._classify_runtime(
        message, "ru", False, False, True, harness.clock(),
    )
    assert code not in detected


@pytest.mark.parametrize(
    "message,code", [
        ("Менеджер", "human_request"),
        ("Позовите человека", "human_request"),
        ("Дайте скидку", "discount"),
        ("Вопрос по гарантии", "warranty"),
        ("Хочу оставить жалобу", "complaint"),
        ("Можно забрать?", "pickup"),
        ("Qaytaring", "return"),
    ],
)
def test_enabled_sheet_intents_extend_but_do_not_weaken_builtin_matching(
    tmp_path, message, code,
):
    from telegram_business.sheets import INTENT_SEED, IntentOverride

    class SeedIntents:
        def intents_cached(self):
            return tuple(
                IntentOverride(
                    row[0], row[1], row[2], row[3],
                    tuple(row[4].split(";")), tuple(row[5].split(";")),
                    tuple(row[6].split(";")) if row[6] else (),
                    row[7], row[8], row[9],
                )
                for row in INTENT_SEED
            )

    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.service.sheets = SeedIntents()
    detected = harness.service._classify_runtime(
        message, "ru", False, False, True, harness.clock(),
    )
    assert code in detected


@pytest.mark.parametrize("message", ["я не беру iPhone", "не доставьте пока"])
def test_negated_order_request_is_not_marked_as_order(message):
    assert "order_request" not in classify(message)


def test_ambiguous_language_evidence_uses_saved_language():
    assert detect_language("кредит", saved="uz", telegram_code="uz")[0] == "uz"
    assert detect_language("цена qancha", saved="uz", telegram_code="ru")[0] == "uz"
    assert "return" in classify("пулни қайтариш керак")
    assert "complaint" in classify("шикоятим бор, маҳсулотда нуқсон")
    assert "human_request" in classify("менежер билан гаплашаман")
    assert "discount" in classify("чегирма борми?")


def test_connection_without_can_reply_is_recorded_but_not_scheduled(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.service.repo.upsert_connection(
        {"id": "connection", "user": {"id": 100}, "is_enabled": True, "rights": {"can_reply": False}},
        harness.clock(),
    )
    harness.incoming(harness.clock(), "iPhone 16 Pro Max")
    harness.clock.value += timedelta(seconds=3)
    assert harness.service.repo.due_actions(harness.clock()) == []
    with connect(harness.service.repo.path) as db:
        assert db.execute("SELECT count(*) FROM business_messages WHERE sender_type='client'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "message_fields,expected_sender",
    [
        ({"sender_business_bot": {"id": 999}}, "business_bot"),
        ({"is_from_offline": True}, "telegram_auto"),
    ],
)
def test_business_bot_and_offline_messages_never_count_as_manager(
    tmp_path, message_fields, expected_sender,
):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    session = harness.incoming(harness.clock(), "Здравствуйте")
    harness.update_id += 1
    outbound = {
        "update_id": harness.update_id,
        "business_message": {
            "business_connection_id": "connection",
            "message_id": 900 + harness.update_id,
            "date": int(harness.clock().timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 100},
            "text": "Служебный исходящий ответ",
            **message_fields,
        },
    }
    assert harness.service.repo.save_update(outbound, harness.clock())
    harness.service.process_update(outbound)

    client = harness.service.repo.client("200")
    assert client["manager_lock_until"] is None
    with connect(harness.service.repo.path) as db:
        sender = db.execute(
            "SELECT sender_type FROM business_messages WHERE message_id=?",
            (902,),
        ).fetchone()["sender_type"]
        cycle = db.execute(
            "SELECT status FROM response_cycles WHERE chat_id='200'",
        ).fetchone()["status"]
        pending = db.execute(
            "SELECT count(*) FROM scheduled_actions WHERE chat_id='200' AND status='pending'",
        ).fetchone()[0]
    assert sender == expected_sender
    assert cycle == "waiting_manager"
    assert pending == 1
    assert harness.service.repo.session_by_id(session["session_id"])["needs_manager_reply"] == 1


@pytest.mark.parametrize(
    "message_fields,expected_type,expected_file_id",
    [
        (
            {"photo": [
                {"file_id": "photo-small", "width": 100, "height": 100},
                {"file_id": "photo-large", "width": 1000, "height": 1000},
            ]},
            "photo",
            "photo-large",
        ),
        ({"voice": {"file_id": "voice-file", "duration": 3}}, "voice", "voice-file"),
        ({"animation": {"file_id": "animation-file"}}, "animation", "animation-file"),
        ({"video_note": {"file_id": "round-video-file"}}, "video_note", "round-video-file"),
        (
            {"paid_media": {"star_count": 1, "paid_media": [{
                "type": "photo",
                "photo": [
                    {"file_id": "paid-small"},
                    {"file_id": "paid-large"},
                ],
            }]}},
            "paid_media",
            "paid-large",
        ),
        ({"story": {"chat": {"id": 200}, "id": 77}}, "story", None),
    ],
)
def test_media_without_text_is_saved_and_answered_for_manager(
    tmp_path, message_fields, expected_type, expected_file_id,
):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.incoming(harness.clock(), message_fields=message_fields)
    harness.run_debounce()
    assert bot_templates(harness) == ["media_only"]
    assert "менеджер" in harness.api.sent[0]["text"].casefold()
    assert harness.products.queries == []
    with connect(harness.service.repo.path) as db:
        saved = db.execute(
            "SELECT message_type,file_id FROM business_messages WHERE sender_type='client'",
        ).fetchone()
    assert saved["message_type"] == expected_type
    assert saved["file_id"] == expected_file_id


def test_client_edit_unknown_to_valid_restarts_durable_debounce_once(tmp_path):
    original_at = datetime(2026, 8, 22, 19, 59, 50, tzinfo=TZ)
    harness = Harness(tmp_path, original_at)
    harness.incoming(original_at, "UnknownPhone ZX999")
    harness.run_debounce()
    with connect(harness.service.repo.path) as db:
        original_action = db.execute(
            "SELECT * FROM scheduled_actions WHERE status='running'"
        ).fetchone()
    assert harness.service.repo.finish_action(
        original_action["action_id"], harness.clock(),
        lease_token=original_action["lease_token"],
        generation=original_action["generation"],
    )
    assert harness.api.sent == []  # original was processed in daytime mode

    edit_at = datetime(2026, 8, 22, 20, 0, 0, tzinfo=TZ)
    harness.clock.value = edit_at
    harness.update_id += 1
    edited = {
        "update_id": harness.update_id,
        "edited_business_message": {
            "business_connection_id": "connection",
            "message_id": 1,
            "date": int(original_at.timestamp()),
            "edit_date": int(edit_at.timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "language_code": "ru"},
            "text": "iPhone 16 Pro Max",
        },
    }
    assert harness.service.repo.save_update(edited, harness.clock())
    harness.service.process_update(edited)

    with connect(harness.service.repo.path) as db:
        revision_actions = db.execute(
            """SELECT * FROM scheduled_actions WHERE action_type='debounce'
               AND json_extract(payload,'$.edit_update_id')=?""",
            (harness.update_id,),
        ).fetchall()
        saved = db.execute(
            "SELECT * FROM business_messages WHERE message_id=1",
        ).fetchone()
    assert len(revision_actions) == 1
    assert datetime.fromisoformat(saved["telegram_date"]) == original_at
    assert datetime.fromisoformat(saved["edited_at"]) == edit_at

    harness.clock.value += timedelta(seconds=3)
    claimed = [
        row for row in harness.service.repo.claim_due_actions(harness.clock())
        if json.loads(row["payload"]).get("edit_update_id") == harness.update_id
    ]
    assert len(claimed) == 1
    action = claimed[0]
    with harness.service.repo.bind_action_claim(
        action["action_id"], action["lease_token"], action["generation"],
    ):
        harness.service.execute(action)
    assert harness.service.repo.finish_action(
        action["action_id"], harness.clock(),
        lease_token=action["lease_token"], generation=action["generation"],
    )
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    assert bot_templates(harness).count("product_result") == 1

    # Simulate a restart retry after the action already completed. The same
    # edit revision is a durable receipt and must not be resurrected.
    harness.service.process_update(edited)
    harness.clock.value += timedelta(seconds=3)
    assert harness.service.repo.due_actions(harness.clock()) == []
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    assert bot_templates(harness).count("product_result") == 1


def test_edit_before_original_uses_revision_anchor_once_across_retries(tmp_path):
    original_at = datetime(2026, 8, 22, 21, 0, 0, tzinfo=TZ)
    edit_at = original_at + timedelta(seconds=10)
    harness = Harness(tmp_path, edit_at + timedelta(seconds=1))

    edited = {
        "update_id": 2,
        "edited_business_message": {
            "business_connection_id": "connection", "message_id": 1,
            "date": int(original_at.timestamp()),
            "edit_date": int(edit_at.timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "language_code": "ru"},
            "text": "iPhone 16 Pro Max",
        },
    }
    assert harness.service.repo.save_update(edited, harness.clock())
    harness.service.process_update(edited)
    assert harness.service.repo.due_actions(harness.clock()) == []

    original = {
        "update_id": 1,
        "business_message": {
            "business_connection_id": "connection", "message_id": 1,
            "date": int(original_at.timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "language_code": "ru"},
            "text": "UnknownPhone ZX999",
        },
    }
    assert harness.service.repo.save_update(original, harness.clock())
    harness.service.process_update(original)
    with connect(harness.service.repo.path) as db:
        actions = db.execute(
            "SELECT * FROM scheduled_actions WHERE action_type='debounce'"
        ).fetchall()
        saved = db.execute(
            "SELECT * FROM business_messages WHERE message_id=1"
        ).fetchone()
    assert len(actions) == 1
    payload = json.loads(actions[0]["payload"])
    assert payload["event_at"] == edit_at.isoformat()
    assert payload["edit_update_id"] == 2
    assert saved["text"] == "iPhone 16 Pro Max"
    assert saved["update_id"] == 1
    assert saved["edit_update_id"] == 2

    harness.clock.value += timedelta(seconds=3)
    action = harness.service.repo.claim_due_actions(harness.clock())[0]
    with harness.service.repo.bind_action_claim(
        action["action_id"], action["lease_token"], action["generation"],
    ):
        harness.service.execute(action)
    assert harness.service.repo.finish_action(
        action["action_id"], harness.clock(),
        lease_token=action["lease_token"], generation=action["generation"],
    )
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    assert bot_templates(harness).count("greeting_model") == 1
    assert bot_templates(harness).count("product_result") == 1

    # Either durable update can be retried after a crash. Both converge on the
    # same edit revision receipt and cannot create another action/response.
    harness.service.process_update(original)
    harness.service.process_update(edited)
    harness.clock.value += timedelta(seconds=3)
    assert harness.service.repo.due_actions(harness.clock()) == []
    assert harness.products.queries == [("iphone 16 pro max", None, None)]
    assert bot_templates(harness).count("product_result") == 1
    with connect(harness.service.repo.path) as db:
        assert db.execute(
            """SELECT count(*) FROM scheduled_actions
               WHERE json_extract(payload,'$.edit_update_id')=2"""
        ).fetchone()[0] == 1


def test_client_edit_after_manager_answer_does_not_restart_automation(tmp_path):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    original_at = harness.clock()
    harness.incoming(original_at, "UnknownPhone ZX999")

    manager_at = original_at + timedelta(seconds=1)
    harness.clock.value = manager_at
    harness.update_id += 1
    manager = {
        "update_id": harness.update_id,
        "business_message": {
            "business_connection_id": "connection", "message_id": 2,
            "date": int(manager_at.timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 100}, "text": "ответ менеджера",
        },
    }
    assert harness.service.repo.save_update(manager, harness.clock())
    harness.service.process_update(manager)

    edit_at = manager_at + timedelta(seconds=1)
    harness.clock.value = edit_at
    harness.update_id += 1
    edited = {
        "update_id": harness.update_id,
        "edited_business_message": {
            "business_connection_id": "connection", "message_id": 1,
            "date": int(original_at.timestamp()),
            "edit_date": int(edit_at.timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200}, "text": "iPhone 16 Pro Max",
        },
    }
    assert harness.service.repo.save_update(edited, harness.clock())
    harness.service.process_update(edited)
    with connect(harness.service.repo.path) as db:
        active = db.execute(
            """SELECT count(*) FROM scheduled_actions
               WHERE status IN ('pending','running')"""
        ).fetchone()[0]
        text = db.execute(
            "SELECT text FROM business_messages WHERE message_id=1"
        ).fetchone()[0]
    assert text == "iPhone 16 Pro Max"  # revision remains audited
    assert active == 0


@pytest.mark.parametrize(
    "extra,expected_sender",
    [
        ({"sender_business_bot": {"id": 999}}, "business_bot"),
        ({"is_from_offline": True}, "telegram_auto"),
        ({}, "manager"),
    ],
)
def test_non_client_edits_are_persisted_without_debounce(
    tmp_path, extra, expected_sender,
):
    harness = Harness(tmp_path, datetime(2026, 8, 22, 21, 0, tzinfo=TZ))
    harness.update_id += 1
    original = {
        "update_id": harness.update_id,
        "business_message": {
            "business_connection_id": "connection", "message_id": 50,
            "date": int(harness.clock().timestamp()),
            "chat": {"id": 300, "type": "private"},
            "from": {"id": 100}, "text": "исходящий текст", **extra,
        },
    }
    assert harness.service.repo.save_update(original, harness.clock())
    harness.service.process_update(original)

    harness.clock.value += timedelta(seconds=1)
    harness.update_id += 1
    edited_message = {
        **original["business_message"],
        "edit_date": int(harness.clock().timestamp()),
        "text": "изменённый исходящий текст",
    }
    edited = {
        "update_id": harness.update_id,
        "edited_business_message": edited_message,
    }
    assert harness.service.repo.save_update(edited, harness.clock())
    harness.service.process_update(edited)
    with connect(harness.service.repo.path) as db:
        saved = db.execute(
            "SELECT * FROM business_messages WHERE chat_id='300' AND message_id=50"
        ).fetchone()
        actions = db.execute(
            "SELECT count(*) FROM scheduled_actions WHERE chat_id='300'"
        ).fetchone()[0]
    assert saved["sender_type"] == expected_sender
    assert saved["text"] == "изменённый исходящий текст"
    assert actions == 0


def test_real_bot_urls_path_and_known_product_mapping():
    path = Path(__file__).resolve().parent / "data" / "Bot_URLS.xlsx"
    assert path.is_file(), "case-sensitive data/Bot_URLS.xlsx path is missing"
    links = load_product_urls(path)
    assert len(links) > 9000
    assert links[3524] == "https://t.me/Texnikach_Phone/1293"
    assert links[5414] == "https://t.me/Texnikach_Phone/1553"


def test_model_url_fallback_requires_one_unambiguous_trusted_post(tmp_path):
    path = tmp_path / "Bot_URLS.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["\ufeffPOST_ID", "Product_ID", "Model"])
    sheet.append(["https://t.me/Texnikach_Phone/10", 0, "Apple iPhone Test"])
    sheet.append(["https://t.me/Texnikach_Phone/11", None, "Duplicate Phone"])
    sheet.append(["https://t.me/Texnikach_dop/12", None, "Duplicate Phone"])
    sheet.append(["https://evil.example/13", None, "Unsafe Phone"])
    book.save(path)

    assert load_product_urls(path) == {}
    model_links = load_model_urls(path)
    assert model_links["apple iphone test"] == "https://t.me/Texnikach_Phone/10"
    assert model_links["iphone test"] == "https://t.me/Texnikach_Phone/10"
    assert "duplicate phone" not in model_links
    assert "unsafe phone" not in model_links

    catalog = {
        "kurs": Decimal("1"),
        "rows": [{
            "product_id": 0, "model_name": "iPhone Test", "family_name": "iPhone Test",
            "memory": "256 GB", "color": "Black", "price": Decimal("1000000"),
            "warranty_period": 12,
        }],
    }
    import sys
    import types

    fake_instagram = types.ModuleType("instagram_bot")
    fake_instagram.PRODUCTS_CACHE = {"source": "google_sheets"}
    fake_instagram.get_product_catalog = lambda **_: catalog
    with patch.dict(sys.modules, {"instagram_bot": fake_instagram}):
        repo = ExistingGoogleProductRepository(urls_path=path)
        match = repo.search("iPhone Test")
    assert match.status == "found"
    assert match.url_for("iPhone Test") == "https://t.me/Texnikach_Phone/10"


def test_numeric_generation_is_never_changed_by_fuzzy_search():
    repo = ExistingGoogleProductRepository()
    repo._variants = [
        ProductVariant("Apple iPhone 15 Pro Max", "256 GB", "Black", Decimal("1")),
        ProductVariant("Samsung Galaxy S25 Ultra", "256 GB", "Black", Decimal("1")),
    ]
    repo._loaded = system_time.time()
    assert repo.search("iphnoe 16 pro max").status == "not_found"
    assert repo.search("Samsung S26 Ultra").status == "not_found"


def test_family_variants_memory_color_and_airpods_ordinal_search():
    repo = ExistingGoogleProductRepository()
    url = "https://t.me/Texnikach_Music/23"
    repo._variants = [
        ProductVariant("Apple AirPods Pro (2nd generation) Lightning", "", "White", Decimal("100"), 1, url),
        ProductVariant("Apple AirPods Pro (2nd generation) USB-C", "", "White", Decimal("110"), 2, url),
        ProductVariant("Phone X (Sim) 5G", "1024 GB", "Black Titanium", Decimal("200"), 3, url),
        ProductVariant("Phone X (eSim) 5G", "512 GB", "White", Decimal("150"), 4, url),
    ]
    repo._loaded = system_time.time()

    airpods = repo.search("AirPods Pro 2")
    assert airpods.status == "found"
    assert len(airpods.variants) == 2
    filtered = repo.search("Phone X 1TB черном")
    assert filtered.status == "found"
    assert len(filtered.variants) == 1
    assert filtered.variants[0].memory == "1024 GB"
    assert filtered.variants[0].color == "Black Titanium"


def test_unmatched_product_filters_are_explicit_and_safely_formatted():
    repo = ExistingGoogleProductRepository()
    repo._variants = [
        ProductVariant("Phone X", "256 GB", "Black", Decimal("1000000")),
        ProductVariant("Phone X", "512 GB", "White", Decimal("1200000")),
    ]
    repo._loaded = system_time.time()

    missing_memory = repo.search("Phone X", memory="1 TB")
    assert missing_memory.status == "found"
    assert missing_memory.requested_memory == "1 TB"
    assert missing_memory.unmatched_filters == ("memory",)
    assert not missing_memory.filters_matched
    assert len(missing_memory.variants) == 2
    assert "нет точного варианта по памяти 1 TB" in format_result(missing_memory, "ru")

    missing_combination = repo.search("Phone X", memory="256 GB", color="White")
    assert missing_combination.unmatched_filters == ("memory_color_combination",)
    assert "256 GB xotira va White rang kombinatsiyasiga" in format_result(missing_combination, "uz")

    matched = repo.search("Phone X", memory="512 GB", color="White")
    assert matched.filters_matched
    assert matched.unmatched_filters == ()
    assert [(item.memory, item.color) for item in matched.variants] == [("512 GB", "White")]


def test_product_repository_rejects_explicitly_stale_catalog():
    repo = ExistingGoogleProductRepository(max_age_minutes=1)
    repo._variants = [ProductVariant("Phone X", "256 GB", "Black", Decimal("1000000"))]
    repo._loaded = system_time.time() - 61
    with patch.object(repo, "_load", return_value=None):
        with pytest.raises(RuntimeError, match="product prices are stale"):
            repo.search("Phone X")


def test_ambiguous_search_and_keyboard_are_capped_at_five():
    repo = ExistingGoogleProductRepository()
    repo._variants = [
        ProductVariant(
            f"Samsung Galaxy S25 Variant {number}", "256 GB", "Black", Decimal(number),
            number, f"https://t.me/Texnikach_Phone/{100 + number}",
        )
        for number in range(1, 8)
    ]
    repo._loaded = system_time.time()
    match = repo.search("Samsung S25")
    assert match.status == "ambiguous"
    assert len(match.models) == 5
    assert len(model_link_keyboard(match)["inline_keyboard"]) == 5
    assert format_ambiguous_result(match, "ru").count("<a href=") == 5


def test_uzbek_cyrillic_fillers_do_not_pollute_model_query():
    query, memory, color = extract_product_query(
        "самсунг с25 ультра қанча нархи бўлиб тўлаш қора 256"
    )
    assert query == "samsung s25 ultra"
    assert memory == "256 GB"
    assert color == "black"
    assert "қанча" in normalize_model("қанча")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("цвет фиолетовый", "purple"),
        ("цвет мокко", "mocha"),
        ("rang binafsha", "purple"),
        ("цвет натуральный титан", "natural titanium"),
        ("цвет Ultramarine", "ultramarine"),
    ],
)
def test_explicit_color_values_are_captured(message, expected):
    query, _, color = extract_product_query(message)
    assert query == ""
    assert color == expected


def test_known_clickable_catalog_title_is_recognized_without_price_fetch():
    repo = ExistingGoogleProductRepository(urls_path="data/Bot_URLS.xlsx")
    assert repo.recognizes_query("DualSense Wireless Controller")
    assert repo.recognizes_query("Amazfit Balance 2")
    assert not repo.recognizes_query("Как дела?")


def test_price_rounding_matches_existing_half_up_catalog_logic():
    variant = ProductVariant("Phone X", "256 GB", "Black", Decimal("14500500"))
    output = format_result(ProductMatch("found", ("Phone X",), (variant,)), "ru")
    assert "14 501 000 so'm" in output


def test_unmatched_color_keeps_matching_memory_filter():
    repo = ExistingGoogleProductRepository()
    repo._variants = [
        ProductVariant("Phone X", "256 GB", "Black", Decimal("1000000")),
        ProductVariant("Phone X", "512 GB", "White", Decimal("1200000")),
    ]
    repo._loaded = system_time.time()
    match = repo.search("Phone X", memory="256 GB", color="Purple")
    assert match.unmatched_filters == ("color",)
    assert {variant.memory for variant in match.variants} == {"256 GB"}


def test_safe_html_urls_and_utf16_telegram_limit():
    assert safe_product_url("https://t.me/channel/123") == "https://t.me/channel/123"
    assert safe_product_url("https://user@t.me/channel/123") is None
    assert safe_product_url("https://t.me.evil.example/channel/123") is None
    assert safe_product_url("https://t.me/channel/not-a-message") is None
    assert safe_product_url("https://t.me/channel/123\nhttps://evil.example") is None

    url = "https://t.me/Texnikach_Phone/123"
    variants = tuple(
        ProductVariant("<Phone & X>", f"{number} GB", "😀" * 300, Decimal("1000000"), number, url)
        for number in range(1, 13)
    )
    match = ProductMatch("found", ("<Phone & X>",), variants, (("<Phone & X>", url),))
    output = format_result(match, "ru")
    assert "&lt;Phone &amp; X&gt;" in output
    assert "<Phone & X>" not in output
    assert len(output.encode("utf-16-le")) // 2 <= 4096
    assert output.count('<a href="https://t.me/Texnikach_Phone/123">') == 1
