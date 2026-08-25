from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from telegram_business.products import ProductMatch, ProductVariant, normalize_model
from telegram_business.repository import BusinessRepository
from telegram_business.request_coordinator import NightRequestCoordinator


TZ = ZoneInfo("Asia/Tashkent")
CONNECTION_ID = "business-connection"


class Catalogue:
    def __init__(self) -> None:
        self.by_model = {
            "iPhone 16 Pro Max": (
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "256 GB",
                    "Black",
                    Decimal("14500000"),
                    1,
                    "https://t.me/Texnikach_Phone/101",
                ),
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "256 GB",
                    "White",
                    Decimal("14600000"),
                    2,
                    "https://t.me/Texnikach_Phone/102",
                ),
                ProductVariant(
                    "iPhone 16 Pro Max",
                    "512 GB",
                    "Black",
                    Decimal("16200000"),
                    3,
                    "https://t.me/Texnikach_Phone/103",
                ),
            ),
            "iPhone 16 Pro": (
                ProductVariant(
                    "iPhone 16 Pro",
                    "256 GB",
                    "Black",
                    Decimal("13500000"),
                    4,
                    "https://t.me/Texnikach_Phone/104",
                ),
            ),
            "Apple Watch Series 10": (
                ProductVariant(
                    "Apple Watch Series 10",
                    "41mm",
                    "Midnight",
                    Decimal("5000000"),
                    5,
                    "https://t.me/Texnikach_Phone/105",
                ),
                ProductVariant(
                    "Apple Watch Series 10",
                    "45mm",
                    "Starlight",
                    Decimal("5400000"),
                    6,
                    "https://t.me/Texnikach_Phone/106",
                ),
            ),
            "Apple Adapter 20W": (
                ProductVariant(
                    "Apple Adapter 20W",
                    "",
                    "",
                    Decimal("250000"),
                    7,
                    "https://t.me/Texnikach_Phone/107",
                ),
            ),
        }
        self.searches: list[tuple[str, str | None, str | None]] = []

    def search(
        self,
        query: str,
        memory: str | None = None,
        color: str | None = None,
    ) -> ProductMatch:
        self.searches.append((query, memory, color))
        normalized = normalize_model(query)
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
                item
                for item in variants
                if normalize_model(memory) == normalize_model(item.memory)
            )
        if color:
            variants = tuple(
                item
                for item in variants
                if normalize_model(color) == normalize_model(item.color)
            )
        if not variants:
            return ProductMatch("not_found")
        urls = tuple(
            dict.fromkeys(
                (item.model, item.url)
                for item in variants
                if item.url
            )
        )
        return ProductMatch("found", (model,), variants, urls)

    def ambiguous_iphone(self) -> ProductMatch:
        models = ("iPhone 16 Pro Max", "iPhone 16 Pro")
        return ProductMatch(
            "ambiguous",
            models,
            (),
            tuple(
                (model, self.by_model[model][0].url or "")
                for model in models
            ),
        )


class API:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.answers: list[tuple[str, str | None]] = []

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


class Service:
    def __init__(self, repo: BusinessRepository, products: Catalogue, api: API):
        self.repo = repo
        self.products = products
        self.api = api
        self.settings = SimpleNamespace(allowed_connection_id=CONNECTION_ID)
        self.errors: list[tuple] = []

    @staticmethod
    def _runtime_policy(_now: datetime):
        return SimpleNamespace(night_start=time(20), night_end=time(9, 30))

    @staticmethod
    def _connection_allows_reply(connection_id: str) -> bool:
        return connection_id == CONNECTION_ID

    @staticmethod
    def _manager_fence_active(_chat_id: str, _now: datetime, _policy) -> bool:
        return False

    @staticmethod
    def _manager_phrases(_now: datetime, _policy) -> tuple[str, str]:
        return "сегодня после 10:00", "bugun soat 10:00 dan keyin"

    @staticmethod
    def _render_message(code: str, language: str, _now: datetime, **values) -> str:
        missing = values.get("missing_fields") or ""
        return f"{code}:{language}:{missing}"

    def _record_error(self, *args) -> None:
        self.errors.append(args)

    def send(self, *args, **kwargs):  # pragma: no cover - every test edits one screen
        raise AssertionError("wizard unexpectedly sent a second message")


@dataclass
class Harness:
    path: Path
    repo: BusinessRepository
    products: Catalogue
    api: API
    service: Service
    coordinator: NightRequestCoordinator
    now: datetime

    def restart(self) -> "Harness":
        repo = BusinessRepository(self.path)
        api = API()
        service = Service(repo, self.products, api)
        return Harness(
            self.path,
            repo,
            self.products,
            api,
            service,
            NightRequestCoordinator(service),
            self.now,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    path = tmp_path / "business.db"
    repo = BusinessRepository(path)
    products = Catalogue()
    api = API()
    service = Service(repo, products, api)
    now = datetime(2026, 8, 22, 22, 0, tzinfo=TZ)
    return Harness(
        path,
        repo,
        products,
        api,
        service,
        NightRequestCoordinator(service),
        now,
    )


def open_chat(harness: Harness, chat_id: str) -> str:
    harness.repo.upsert_client(
        chat_id,
        {"id": int(chat_id), "first_name": "Client"},
        harness.now,
    )
    session = harness.repo.session(chat_id, harness.now)
    harness.repo.touch_client_message(
        chat_id,
        session["session_id"],
        harness.now,
        event_at=harness.now,
        message_id=1,
    )
    return str(session["session_id"])


def prepare(
    harness: Harness,
    chat_id: str,
    session_id: str,
    match: ProductMatch,
    *,
    model_query: str,
):
    screen = harness.coordinator.prepare_match(
        match,
        connection_id=CONNECTION_ID,
        chat_id=chat_id,
        session_id=session_id,
        language="ru",
        now=harness.now,
        event_at=harness.now,
        message_id=1,
        update_id=100,
        model_query=model_query,
    )
    assert screen is not None
    return screen


def callback_data(markup: dict, text: str) -> str:
    matches = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if text.casefold() in button.get("text", "").casefold()
        and "callback_data" in button
    ]
    assert len(matches) == 1, (text, markup)
    return matches[0]


def click(
    harness: Harness,
    data: str,
    *,
    chat_id: str,
    callback_id: str,
    at: datetime | None = None,
    message_id: int = 700,
) -> dict | None:
    before = len(harness.api.edits)
    handled = harness.coordinator.handle_callback(
        {
            "callback_query": {
                "id": callback_id,
                "from": {"id": int(chat_id)},
                "data": data,
                "message": {
                    "message_id": message_id,
                    "business_connection_id": CONNECTION_ID,
                    "chat": {"id": int(chat_id), "type": "private"},
                },
            }
        },
        at or harness.now,
    )
    assert handled is True
    return harness.api.edits[-1] if len(harness.api.edits) > before else None


def enter_text(
    harness: Harness,
    *,
    chat_id: str,
    session_id: str,
    text: str,
    message_id: int,
    at: datetime,
) -> None:
    assert harness.coordinator.handle_expected_input(
        connection_id=CONNECTION_ID,
        chat_id=chat_id,
        session_id=session_id,
        rows=(),
        text=text,
        language="ru",
        now=at,
        event_at=at,
        update_id=1000 + message_id,
        message_id=message_id,
    )


def test_ambiguous_callback_selects_exact_model_and_fences_old_buttons(
    harness: Harness,
):
    chat_id = "101"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.ambiguous_iphone(),
        model_query="iphone 16 pro",
    )
    first = callback_data(screen.reply_markup, "Выбрать 1")
    second = callback_data(screen.reply_markup, "Выбрать 2")

    edited = click(
        harness,
        first,
        chat_id=chat_id,
        callback_id="ambiguous-first",
    )

    request = harness.repo.active_business_request(chat_id, session_id)
    assert edited is not None
    assert request["exact_model"] == "iPhone 16 Pro Max"
    assert request["wizard_state"] == "memory"
    assert "Выберите память" in edited["text"]

    edit_count = len(harness.api.edits)
    revision = request["revision"]
    assert click(
        harness,
        second,
        chat_id=chat_id,
        callback_id="ambiguous-stale",
    ) is None
    assert len(harness.api.edits) == edit_count
    assert harness.repo.active_business_request(chat_id, session_id)["revision"] == revision
    assert "устарел" in (harness.api.answers[-1][1] or "")

    assert click(
        harness,
        first,
        chat_id=chat_id,
        callback_id="ambiguous-first",
    ) is None
    assert harness.repo.active_business_request(chat_id, session_id)["revision"] == revision
    assert harness.api.answers[-1] == ("ambiguous-first", None)

    assert click(
        harness,
        first,
        chat_id=chat_id,
        callback_id="ambiguous-token-replay",
    ) is None
    assert harness.repo.active_business_request(chat_id, session_id)["revision"] == revision
    assert "устарел" in (harness.api.answers[-1][1] or "")


def test_exact_model_memory_color_delivery_phone_location_and_submit(
    harness: Harness,
):
    chat_id = "102"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("iPhone 16 Pro Max"),
        model_query="iPhone 16 Pro Max",
    )
    assert screen.step.code == "memory"

    edited = click(
        harness,
        callback_data(screen.reply_markup, "256 GB"),
        chat_id=chat_id,
        callback_id="delivery-memory",
    )
    assert edited is not None and "Выберите желаемый цвет" in edited["text"]
    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Black"),
        chat_id=chat_id,
        callback_id="delivery-color",
    )
    assert edited is not None and "доставка или самовывоз" in edited["text"]
    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Доставка"),
        chat_id=chat_id,
        callback_id="delivery-method",
    )
    assert edited is not None and "нужен номер телефона" in edited["text"]

    enter_text(
        harness,
        chat_id=chat_id,
        session_id=session_id,
        text="+998 90 123 45 67",
        message_id=2,
        at=harness.now + timedelta(seconds=10),
    )
    assert "Отправьте локацию" in harness.api.edits[-1]["text"]
    enter_text(
        harness,
        chat_id=chat_id,
        session_id=session_id,
        text="Ташкент, Чиланзар 10",
        message_id=3,
        at=harness.now + timedelta(seconds=20),
    )
    review = harness.api.edits[-1]
    assert "Заказ не оформлен" in review["text"]
    assert "+998901234567" not in review["text"]

    edited = click(
        harness,
        callback_data(review["reply_markup"], "Передать менеджеру"),
        chat_id=chat_id,
        callback_id="delivery-submit",
        at=harness.now + timedelta(seconds=21),
    )
    request = harness.repo.active_business_request(chat_id, session_id)
    assert edited is not None
    assert edited["reply_markup"] == {"inline_keyboard": []}
    assert "request_saved_delivery" in edited["text"]
    assert request["status"] == "submitted"
    assert request["wizard_state"] == "complete_delivery"
    assert request["option_kind"] == "memory"
    assert request["option_value"] == "256 GB"
    assert request["color"] == "Black"
    assert request["phone"] == "+998901234567"
    assert request["address"] == "Ташкент, Чиланзар 10"
    assert request["needs_manager_reply"] == 1


def test_watch_memory_column_becomes_mm_size(harness: Harness):
    chat_id = "103"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("Apple Watch Series 10"),
        model_query="Apple Watch Series 10",
    )

    assert screen.step.code == "size"
    assert "Выберите размер" in screen.step.text
    edited = click(
        harness,
        callback_data(screen.reply_markup, "41mm"),
        chat_id=chat_id,
        callback_id="watch-size",
    )
    request = harness.repo.active_business_request(chat_id, session_id)
    assert edited is not None
    assert request["option_kind"] == "size"
    assert request["option_value"] == "41mm"
    assert request["wizard_state"] == "color"


def test_product_without_memory_or_color_skips_both_steps(harness: Harness):
    chat_id = "104"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("Apple Adapter 20W"),
        model_query="Apple Adapter 20W",
    )

    request = harness.repo.active_business_request(chat_id, session_id)
    assert screen.step.code == "fulfillment"
    assert request["wizard_state"] == "fulfillment"
    assert request["option_kind"] is None
    assert request["option_value"] is None
    assert request["color"] is None
    texts = [
        button["text"]
        for row in screen.reply_markup["inline_keyboard"]
        for button in row
    ]
    assert not any("памят" in text.casefold() for text in texts)
    assert not any("цвет" in text.casefold() for text in texts)


def test_color_any_and_pickup_submit_without_phone_or_location(harness: Harness):
    chat_id = "105"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("iPhone 16 Pro Max"),
        model_query="iPhone 16 Pro Max",
    )
    edited = click(
        harness,
        callback_data(screen.reply_markup, "256 GB"),
        chat_id=chat_id,
        callback_id="pickup-memory",
    )
    assert edited is not None
    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Цвет не важен"),
        chat_id=chat_id,
        callback_id="pickup-any-color",
    )
    assert edited is not None
    request = harness.repo.active_business_request(chat_id, session_id)
    assert request["color"] is None
    assert request["color_any"] == 1

    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Самовывоз"),
        chat_id=chat_id,
        callback_id="pickup-method",
    )
    assert edited is not None and "телефон необязателен" in edited["text"]
    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Оставить Telegram"),
        chat_id=chat_id,
        callback_id="pickup-telegram",
    )
    assert edited is not None and "этот Telegram-чат" in edited["text"]
    request = harness.repo.active_business_request(chat_id, session_id)
    assert request["wizard_state"] == "review"
    assert request["phone"] is None
    assert request["location_url"] is None
    assert request["address"] is None

    edited = click(
        harness,
        callback_data(edited["reply_markup"], "Передать менеджеру"),
        chat_id=chat_id,
        callback_id="pickup-submit",
    )
    request = harness.repo.active_business_request(chat_id, session_id)
    assert edited is not None
    assert request["status"] == "submitted"
    assert request["wizard_state"] == "complete_pickup"
    assert request["phone"] is None
    assert request["location_url"] is None
    assert request["address"] is None


def test_callback_survives_restart_then_request_expires_and_buttons_go_stale(
    harness: Harness,
):
    chat_id = "106"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("Apple Adapter 20W"),
        model_query="Apple Adapter 20W",
    )
    delivery = callback_data(screen.reply_markup, "Доставка")

    restarted = harness.restart()
    edited = click(
        restarted,
        delivery,
        chat_id=chat_id,
        callback_id="restart-delivery",
        at=harness.now + timedelta(minutes=1),
    )
    assert edited is not None
    assert restarted.repo.active_business_request(chat_id, session_id)[
        "wizard_state"
    ] == "delivery_phone"
    back = callback_data(edited["reply_markup"], "Назад")

    request = restarted.repo.active_business_request(chat_id, session_id)
    expires_at = datetime(2026, 8, 23, 9, 30, tzinfo=TZ)
    restarted.coordinator.expire(
        request["request_id"],
        CONNECTION_ID,
        expires_at,
    )
    expired = restarted.repo.business_request(request["request_id"])
    assert expired["status"] == "expired"
    assert restarted.api.edits[-1]["reply_markup"] == {"inline_keyboard": []}
    assert "request_partial_saved" in restarted.api.edits[-1]["text"]

    edit_count = len(restarted.api.edits)
    assert click(
        restarted,
        back,
        chat_id=chat_id,
        callback_id="expired-back",
        at=expires_at,
    ) is None
    assert len(restarted.api.edits) == edit_count
    assert "устарел" in (restarted.api.answers[-1][1] or "")


def test_manager_closure_revokes_pending_wizard_callbacks(harness: Harness):
    chat_id = "107"
    session_id = open_chat(harness, chat_id)
    screen = prepare(
        harness,
        chat_id,
        session_id,
        harness.products.search("Apple Adapter 20W"),
        model_query="Apple Adapter 20W",
    )
    pickup = callback_data(screen.reply_markup, "Самовывоз")
    manager_at = harness.now + timedelta(minutes=1)

    harness.repo.manager_answer(
        chat_id,
        manager_at,
        120,
        event_at=manager_at,
        message_id=99,
    )

    request = harness.repo.business_request(screen.request["request_id"])
    assert request["status"] == "manager_closed"
    assert click(
        harness,
        pickup,
        chat_id=chat_id,
        callback_id="manager-closed-pickup",
        at=manager_at,
    ) is None
    assert harness.api.edits == []
    assert "устарел" in (harness.api.answers[-1][1] or "")
