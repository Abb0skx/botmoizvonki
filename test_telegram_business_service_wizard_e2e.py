from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from telegram_business.config import BusinessSettings
from telegram_business.migrations import connect
from telegram_business.products import ProductMatch, ProductVariant, normalize_model
from telegram_business.service import BusinessService


TZ = ZoneInfo("Asia/Tashkent")
CONNECTION_ID = "connection"


def make_settings(path: Path, **changes) -> BusinessSettings:
    base = BusinessSettings(
        False,
        "999:token",
        "secret",
        CONNECTION_ID,
        "",
        "Asia/Tashkent",
        time(20),
        time(9, 30),
        time(10),
        time(20),
        300,
        3,
        120,
        720,
        4,
        8,
        path,
        "sheet",
        60,
        300,
        "existing_google_bot_prices",
        "",
        1440,
    )
    return replace(base, **changes)


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class TelegramAPI:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[tuple[str, str | None]] = []

    def send_message(
        self,
        connection_id: str,
        chat_id: str,
        text: str,
        **options,
    ) -> dict:
        message_id = 1000 + len(self.sent) + 1
        self.sent.append(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                **options,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    def edit_message_text(
        self,
        connection_id: str,
        chat_id: str,
        message_id: int,
        text: str,
        **options,
    ) -> dict:
        self.edits.append(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                **options,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> dict:
        self.answers.append((callback_query_id, text))
        return {"ok": True, "result": True}

    @staticmethod
    def get_business_connection(connection_id: str) -> dict:
        return {
            "id": connection_id,
            "user": {"id": 100},
            "is_enabled": True,
            "rights": {"can_reply": True},
        }


class Catalogue:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None, str | None]] = []
        self.by_model = {
            "iPhone 16 Pro Max": (
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "256 GB",
                    "Black",
                    Decimal("14500000"),
                    1,
                    "https://t.me/Texnikach_Phone/201",
                ),
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "256 GB",
                    "White",
                    Decimal("14600000"),
                    2,
                    "https://t.me/Texnikach_Phone/202",
                ),
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "512 GB",
                    "Black",
                    Decimal("16200000"),
                    3,
                    "https://t.me/Texnikach_Phone/203",
                ),
            ),
            "Samsung Galaxy S25": (
                ProductVariant(
                    "Samsung Galaxy S25",
                    "256 GB",
                    "Black",
                    Decimal("9000000"),
                    4,
                    "https://t.me/Texnikach_Phone/204",
                ),
            ),
            "Samsung Galaxy S25 Plus": (
                ProductVariant(
                    "Samsung Galaxy S25 Plus",
                    "256 GB",
                    "Black",
                    Decimal("10000000"),
                    5,
                    "https://t.me/Texnikach_Phone/205",
                ),
            ),
            "Apple Watch Series 10": (
                ProductVariant(
                    "Apple Watch Series 10",
                    "41mm",
                    "Midnight",
                    Decimal("5000000"),
                    6,
                    "https://t.me/Texnikach_Phone/206",
                ),
                ProductVariant(
                    "Apple Watch Series 10",
                    "45mm",
                    "Starlight",
                    Decimal("5400000"),
                    7,
                    "https://t.me/Texnikach_Phone/207",
                ),
            ),
            "Apple Adapter 20W": (
                ProductVariant(
                    "Apple Adapter 20W",
                    "",
                    "",
                    Decimal("250000"),
                    8,
                    "https://t.me/Texnikach_Phone/208",
                ),
            ),
        }

    def recognizes_query(self, query: str) -> bool:
        normalized = normalize_model(query)
        return any(normalize_model(model) == normalized for model in self.by_model)

    def search(
        self,
        query: str,
        memory: str | None = None,
        color: str | None = None,
    ) -> ProductMatch:
        self.queries.append((query, memory, color))
        normalized = normalize_model(query)
        if normalized == "samsung galaxy s25":
            models = ("Samsung Galaxy S25", "Samsung Galaxy S25 Plus")
            return ProductMatch(
                "ambiguous",
                models,
                (),
                tuple(
                    (model, self.by_model[model][0].url or "")
                    for model in models
                ),
            )
        model = next(
            (
                candidate
                for candidate in self.by_model
                if normalize_model(candidate) == normalized
            ),
            None,
        )
        if model is None:
            return ProductMatch("not_found")
        variants = self.by_model[model]
        if memory:
            variants = tuple(
                variant
                for variant in variants
                if normalize_model(variant.memory) == normalize_model(memory)
            )
        if color:
            variants = tuple(
                variant
                for variant in variants
                if normalize_model(variant.color) == normalize_model(color)
            )
        if not variants:
            return ProductMatch("not_found")
        urls = tuple(
            dict.fromkeys(
                (variant.model, variant.url)
                for variant in variants
                if variant.url
            )
        )
        return ProductMatch("found", (model,), variants, urls)


@dataclass
class Harness:
    service: BusinessService
    api: TelegramAPI
    products: Catalogue
    clock: Clock
    update_id: int = 0

    @classmethod
    def create(cls, root: Path) -> "Harness":
        now = datetime(2026, 8, 22, 21, 0, tzinfo=TZ)
        clock = Clock(now)
        api = TelegramAPI()
        products = Catalogue()
        service = BusinessService(
            make_settings(root / "business.db"),
            clock=clock,
            api=api,
            products=products,
        )
        service.repo.upsert_connection(
            {
                "id": CONNECTION_ID,
                "user": {"id": 100},
                "is_enabled": True,
                "rights": {"can_reply": True},
            },
            now,
        )
        return cls(service, api, products, clock)

    def incoming(
        self,
        text: str | None = None,
        *,
        chat_id: int = 300,
        location: dict | None = None,
        contact: dict | None = None,
        language_code: str = "ru",
    ):
        self.update_id += 1
        message = {
            "business_connection_id": CONNECTION_ID,
            "message_id": self.update_id,
            "date": int(self.clock.value.timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "language_code": language_code},
        }
        if text is not None:
            message["text"] = text
        if location is not None:
            message["location"] = location
        if contact is not None:
            message["contact"] = contact
        update = {"update_id": self.update_id, "business_message": message}
        assert self.service.repo.save_update(
            update,
            self.clock.value,
            allowed_connection_id=CONNECTION_ID,
        )
        self.service.process_update(update)
        return self.service.repo.session(
            str(chat_id), self.clock.value, time(20), time(9, 30)
        )

    def run_debounce(self, seconds: int = 3) -> None:
        self.clock.value += timedelta(seconds=seconds)
        actions = [
            row
            for row in self.service.repo.due_actions(self.clock.value)
            if row["action_type"] == "debounce"
        ]
        assert actions
        action = actions[-1]
        assert self.service.repo.claim_action(
            action["action_id"],
            self.clock.value,
            expected_generation=action["generation"],
        )
        self.service.execute(action)

    def callback(
        self,
        data: str,
        *,
        chat_id: int,
        message_id: int,
        callback_id: str,
    ) -> None:
        self.update_id += 1
        update = {
            "update_id": self.update_id,
            "callback_query": {
                "id": callback_id,
                "from": {"id": chat_id},
                "data": data,
                "message": {
                    "message_id": message_id,
                    "business_connection_id": CONNECTION_ID,
                    "chat": {"id": chat_id, "type": "private"},
                },
            },
        }
        assert self.service.repo.save_update(
            update,
            self.clock.value,
            allowed_connection_id=CONNECTION_ID,
        )
        self.service.process_update(update)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness.create(tmp_path)


def callback_data(markup: dict, text: str) -> str:
    values = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if text.casefold() in button.get("text", "").casefold()
        and "callback_data" in button
    ]
    assert len(values) == 1, (text, markup)
    assert values[0].startswith("nr1:")
    return values[0]


def selection_fields(request) -> dict:
    return json.loads(request["selection_fields"] or "{}")


def test_night_business_message_exact_search_creates_request_and_keyboard(
    harness: Harness,
):
    session = harness.incoming("iPhone 16 Pro Max", chat_id=301)
    harness.run_debounce()

    request = harness.service.repo.active_business_request(
        "301", session["session_id"]
    )
    product = harness.api.sent[-1]
    assert request is not None
    assert request["exact_model"] == "iPhone 16 Pro Max"
    assert request["wizard_state"] == "memory"
    assert product["reply_markup"]["inline_keyboard"]
    assert callback_data(product["reply_markup"], "256 GB")
    assert selection_fields(request)["wizard_message_id"] == product["message_id"]
    assert all(
        not button.get("callback_data")
        or button["callback_data"].startswith("nr1:")
        for row in product["reply_markup"]["inline_keyboard"]
        for button in row
    )


def test_night_ambiguous_search_creates_model_callback_buttons(harness: Harness):
    session = harness.incoming("Samsung Galaxy S25", chat_id=302)
    harness.run_debounce()

    request = harness.service.repo.active_business_request(
        "302", session["session_id"]
    )
    sent = harness.api.sent[-1]
    assert request["wizard_state"] == "model"
    assert request["exact_model"] is None
    assert "несколько похожих моделей" in sent["text"]
    assert callback_data(sent["reply_markup"], "Выбрать 1")
    assert callback_data(sent["reply_markup"], "Выбрать 2")


def test_exact_product_without_attributes_starts_at_fulfillment(harness: Harness):
    session = harness.incoming("Apple Adapter 20W", chat_id=303)
    harness.run_debounce()

    request = harness.service.repo.active_business_request(
        "303", session["session_id"]
    )
    sent = harness.api.sent[-1]
    assert request["exact_model"] == "Apple Adapter 20W"
    assert request["wizard_state"] == "fulfillment"
    assert request["option_kind"] is None
    assert request["color"] is None
    assert callback_data(sent["reply_markup"], "Доставка")
    assert callback_data(sent["reply_markup"], "Самовывоз")


def test_process_update_callbacks_exact_to_pickup_telegram_and_submit(
    harness: Harness,
):
    chat_id = 304
    session = harness.incoming("iPhone 16 Pro Max", chat_id=chat_id)
    harness.run_debounce()
    visible_message_id = harness.api.sent[-1]["message_id"]
    markup = harness.api.sent[-1]["reply_markup"]

    harness.callback(
        callback_data(markup, "256 GB"),
        chat_id=chat_id,
        message_id=visible_message_id,
        callback_id="service-memory",
    )
    markup = harness.api.edits[-1]["reply_markup"]
    harness.callback(
        callback_data(markup, "Black"),
        chat_id=chat_id,
        message_id=visible_message_id,
        callback_id="service-color",
    )
    markup = harness.api.edits[-1]["reply_markup"]
    harness.callback(
        callback_data(markup, "Самовывоз"),
        chat_id=chat_id,
        message_id=visible_message_id,
        callback_id="service-pickup",
    )
    markup = harness.api.edits[-1]["reply_markup"]
    harness.callback(
        callback_data(markup, "Оставить Telegram"),
        chat_id=chat_id,
        message_id=visible_message_id,
        callback_id="service-telegram",
    )
    markup = harness.api.edits[-1]["reply_markup"]
    harness.callback(
        callback_data(markup, "Передать менеджеру"),
        chat_id=chat_id,
        message_id=visible_message_id,
        callback_id="service-submit",
    )

    request = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    assert request["status"] == "submitted"
    assert request["wizard_state"] == "complete_pickup"
    assert request["option_value"] == "256 GB"
    assert request["color"] == "Black"
    assert request["phone"] is None
    assert request["location_url"] is None
    assert harness.api.edits[-1]["reply_markup"] == {"inline_keyboard": []}
    assert all(answer[1] is None for answer in harness.api.answers)


@pytest.mark.parametrize(
    ("first_message", "location", "field", "expected"),
    [
        (
            None,
            {"latitude": 41.311081, "longitude": 69.240562},
            "location_url",
            "https://www.google.com/maps?q=41.311081,69.240562",
        ),
        ("+998 90 123 45 67", None, "phone", "+998901234567"),
    ],
)
def test_location_or_phone_sent_before_model_persists_after_exact_search(
    harness: Harness,
    first_message,
    location,
    field,
    expected,
):
    chat_id = 305
    session = harness.incoming(first_message, chat_id=chat_id, location=location)
    harness.run_debounce()
    initial = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    assert initial["wizard_state"] == "model"
    assert initial[field] == expected

    harness.clock.value += timedelta(seconds=10)
    harness.incoming("iPhone 16 Pro Max", chat_id=chat_id)
    harness.run_debounce()
    updated = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    assert updated["exact_model"] == "iPhone 16 Pro Max"
    assert updated[field] == expected


@pytest.mark.parametrize("native_order", [("contact", "location"), ("location", "contact")])
def test_contact_and_location_in_one_burst_are_both_preserved(
    harness: Harness,
    native_order,
):
    chat_id = 3051
    values = {
        "contact": {
            "phone_number": "+998 90 123 45 67",
            "first_name": "Client",
            "user_id": chat_id,
        },
        "location": {"latitude": 41.311081, "longitude": 69.240562},
    }
    session = None
    for kind in native_order:
        session = harness.incoming(
            chat_id=chat_id,
            contact=values[kind] if kind == "contact" else None,
            location=values[kind] if kind == "location" else None,
        )
        harness.clock.value += timedelta(seconds=1)

    harness.run_debounce()
    request = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"],
    )
    assert request["phone"] == "+998901234567"
    assert request["location_url"] == (
        "https://www.google.com/maps?q=41.311081,69.240562"
    )


def test_duplicate_exact_search_keeps_visible_callback_valid(harness: Harness):
    chat_id = 306
    session = harness.incoming("iPhone 16 Pro Max", chat_id=chat_id)
    harness.run_debounce()
    visible = harness.api.sent[-1]
    token = callback_data(visible["reply_markup"], "256 GB")
    request = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    revision = request["revision"]
    sent_count = len(harness.api.sent)
    with connect(harness.service.repo.path) as db:
        product_count = db.execute(
            """SELECT count(*) FROM business_messages
               WHERE chat_id=? AND template_code='product_result'""",
            (str(chat_id),),
        ).fetchone()[0]
    assert product_count == 1

    harness.clock.value += timedelta(minutes=1)
    harness.incoming("iPhone 16 Pro Max", chat_id=chat_id)
    harness.run_debounce()
    after_duplicate = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    assert after_duplicate["revision"] == revision
    assert len(harness.api.sent) == sent_count
    with connect(harness.service.repo.path) as db:
        assert db.execute(
            """SELECT count(*) FROM business_messages
               WHERE chat_id=? AND template_code='product_result'""",
            (str(chat_id),),
        ).fetchone()[0] == 1

    harness.callback(
        token,
        chat_id=chat_id,
        message_id=visible["message_id"],
        callback_id="visible-token-after-duplicate",
    )
    transitioned = harness.service.repo.active_business_request(
        str(chat_id), session["session_id"]
    )
    assert transitioned["wizard_state"] == "color"
    assert transitioned["option_value"] == "256 GB"
    assert harness.api.answers[-1] == ("visible-token-after-duplicate", None)


def test_mm_query_is_extracted_as_watch_size_and_removed_from_model_query(
    harness: Harness,
):
    session = harness.incoming("Apple Watch Series 10 45mm", chat_id=307)
    harness.run_debounce()

    request = harness.service.repo.active_business_request(
        "307", session["session_id"]
    )
    assert harness.products.queries[0] == (
        "apple watch series 10",
        "45mm",
        None,
    )
    assert request["exact_model"] == "Apple Watch Series 10"
    assert request["option_kind"] == "size"
    assert request["option_value"] == "45mm"
    assert request["wizard_state"] == "color"


def test_phone_embedded_alongside_model_is_removed_from_query_and_persisted(
    harness: Harness,
):
    session = harness.incoming(
        "iPhone 16 Pro Max +998 90 123 45 67",
        chat_id=308,
    )
    harness.run_debounce()

    request = harness.service.repo.active_business_request(
        "308", session["session_id"]
    )
    assert harness.products.queries[0] == ("iphone 16 pro max", None, None)
    assert request["exact_model"] == "iPhone 16 Pro Max"
    assert request["phone"] == "+998901234567"
    assert request["contact_method"] == "typed"


def test_callback_updates_are_processed_through_durable_service_rows(
    harness: Harness,
):
    chat_id = 309
    harness.incoming("Apple Adapter 20W", chat_id=chat_id)
    harness.run_debounce()
    visible = harness.api.sent[-1]
    harness.callback(
        callback_data(visible["reply_markup"], "Самовывоз"),
        chat_id=chat_id,
        message_id=visible["message_id"],
        callback_id="durable-service-callback",
    )

    with connect(harness.service.repo.path) as db:
        update = db.execute(
            "SELECT * FROM business_updates WHERE callback_query_id=?",
            ("durable-service-callback",),
        ).fetchone()
        receipt = db.execute(
            "SELECT * FROM business_callback_receipts WHERE callback_query_id=?",
            ("durable-service-callback",),
        ).fetchone()
    assert update["status"] == "processed"
    assert receipt["status"] == "applied"
    assert receipt["outcome"] == "applied"
