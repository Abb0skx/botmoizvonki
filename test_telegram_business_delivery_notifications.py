from __future__ import annotations

import json
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from app.utils.couriers import COURIERS_BY_ID
from telegram_business.config import BusinessSettings
from telegram_business.delivery_notifications import (
    DeliveryFeedError,
    DeliveryFeedPage,
    DeliveryStatusClient,
    newest_current_events,
    validate_feed_page,
)
from telegram_business.delivery_store import DeliveryNotificationStore
from telegram_business.migrations import connect, migrate
from telegram_business.service import BusinessService
from telegram_business.telegram_api import TelegramAPIError
from telegram_business.templates import REVIEW_URL


TZ = ZoneInfo("Asia/Tashkent")
FEED_ID = "11111111-1111-4111-8111-111111111111"
DEFAULT_COURIER = COURIERS_BY_ID[1799690992]
OLMAS = COURIERS_BY_ID[7636344727]


class FakeTelegramAPI:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, tuple[int, ...]]] = []
        self.actions: list[tuple[str, int]] = []
        self.connection_rights = {
            "can_reply": True,
            "can_delete_sent_messages": True,
        }

    def send_message(self, connection_id, chat_id, text, **_options):
        self.sent.append((connection_id, chat_id, text))
        message_id = 7000 + len(self.sent)
        self.actions.append(("send", message_id))
        return {"ok": True, "result": {"message_id": message_id}}

    def get_business_connection(self, connection_id):
        return {
            "id": connection_id,
            "user": {"id": 100},
            "is_enabled": True,
            "rights": dict(self.connection_rights),
        }

    def delete_business_messages(self, connection_id, message_ids):
        ids = tuple(message_ids)
        self.deleted.append((connection_id, ids))
        self.actions.extend(("delete", message_id) for message_id in ids)
        return {"ok": True, "result": True}


class RetryTelegramAPI(FakeTelegramAPI):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.calls = 0

    def send_message(self, connection_id, chat_id, text, **options):
        self.calls += 1
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return super().send_message(connection_id, chat_id, text, **options)


class FakeFeed:
    def __init__(self, latest=0):
        self.latest = latest
        self.events: list[dict] = []
        self.invalidations: list[dict] = []
        self.calls: list[tuple[int, int]] = []

    def fetch(self, after_event_id, limit=100):
        self.calls.append((after_event_id, limit))
        available = sorted(
            [
                (event["event_id"], False, event)
                for event in self.events
                if event["event_id"] > after_event_id
            ]
            + [
                (item["event_id"], True, item)
                for item in self.invalidations
                if item["event_id"] > after_event_id
            ],
            key=lambda item: item[0],
        )[:limit]
        next_id = available[-1][0] if available else after_event_id
        # The fake has no hidden raw rows; latest is still an independent high
        # water mark so the baseline path can discard historical events.
        if self.latest < next_id:
            self.latest = next_id
        return DeliveryFeedPage(
            tuple(item for _, invalid, item in available if not invalid),
            next_id,
            self.latest,
            next_id < self.latest,
            tuple(item for _, invalid, item in available if invalid),
            FEED_ID,
        )


def business_settings(path: Path) -> BusinessSettings:
    return BusinessSettings(
        False,
        "123:token",
        "secret",
        "connection",
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
        delivery_notifications_enabled=True,
        delivery_notifications_url="http://delivery-stats:8080",
        delivery_notifications_token="internal-secret",
        delivery_notifications_poll_seconds=5,
    )


def delivery_event(event_id: int, order_id: int, status="pending", **extra):
    return {
        "event_id": event_id,
        "order_id": order_id,
        "order_number": str(1000 + order_id),
        "status": status,
        "current_status": status,
        "from_status": "draft" if status == "pending" else "pending",
        "product": "iPhone 16 Pro Max",
        "client_phone": "+998901112233",
        "client_phone_2": "",
        "courier_id": DEFAULT_COURIER.user_id,
        "courier_name": DEFAULT_COURIER.name,
        "courier_phone": DEFAULT_COURIER.phone,
        "created_at": "2026-09-07T10:00:00+05:00",
        **extra,
    }


def establish_chat(service: BusinessService, now: datetime, chat_id="200", phone="+998901112233"):
    incoming_at = now - timedelta(hours=1)
    service.repo.upsert_connection(
        {
            "id": "connection",
            "user": {"id": 100},
            "rights": {
                "can_reply": True,
                "can_delete_sent_messages": True,
            },
        },
        now,
    )
    update = {
        "update_id": int(chat_id),
        "business_message": {
            "business_connection_id": "connection",
            "message_id": 10,
            "date": int(incoming_at.timestamp()),
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id), "language_code": "ru"},
            "text": f"мой номер {phone}",
        },
    }
    assert service.repo.save_update(update, now)
    service.process_update(update)
    service.repo.update_language(chat_id, "ru", 0.99, now)
    session = service.repo.session(chat_id, incoming_at)
    service.repo.record_bot_message(
        "connection",
        chat_id,
        session["session_id"],
        900 + int(chat_id),
        "Здравствуйте",
        "greeting_model",
        incoming_at + timedelta(seconds=1),
        count_as_response=False,
    )
    return session["session_id"]


def test_feed_validation_and_newest_status_selection():
    payload = {
        "feed_instance_id": FEED_ID,
        "events": [
            {
                **delivery_event(11, 1, "pending"),
                "to_status": "pending",
            },
            {
                **delivery_event(12, 1, "on_way"),
                "to_status": "on_way",
            },
        ],
        "invalidations": [],
        "next_after_event_id": 12,
        "latest_event_id": 12,
        "has_more": False,
        "cursor_reset_required": False,
    }
    page = validate_feed_page(payload, 10)
    assert [event["event_id"] for event in newest_current_events(page.events)] == [12]

    with pytest.raises(DeliveryFeedError):
        validate_feed_page({**payload, "next_after_event_id": 9}, 10)
    with pytest.raises(DeliveryFeedError):
        validate_feed_page({**payload, "has_more": True}, 10)

    rollback = validate_feed_page(
        {
            "feed_instance_id": FEED_ID,
            "events": [],
            "invalidations": [{
                "event_id": 13,
                "order_id": 1,
                "current_status": "picked_up",
                "to_status": "picked_up",
                "created_at": "2026-09-07T20:01:00+05:00",
            }],
            "next_after_event_id": 13,
            "latest_event_id": 13,
            "has_more": False,
            "cursor_reset_required": False,
        },
        12,
    )
    assert rollback.invalidations[0]["order_id"] == 1
    with pytest.raises(DeliveryFeedError):
        validate_feed_page(
            {
                **payload,
                "invalidations": [{
                    "event_id": 12,
                    "order_id": 1,
                    "current_status": "",
                    "to_status": "picked_up",
                }],
            },
            10,
        )


def test_delivery_client_never_puts_token_in_url():
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "feed_instance_id": FEED_ID,
                "events": [],
                "invalidations": [],
                "next_after_event_id": 7,
                "latest_event_id": 7,
                "has_more": False,
                "cursor_reset_required": False,
            }

    class HTTP:
        def __init__(self):
            self.call = None

        def get(self, *args, **kwargs):
            self.call = (args, kwargs)
            return Response()

    http = HTTP()
    client = DeliveryStatusClient("http://delivery:8080", "top-secret", http=http)
    page = client.fetch(7)
    assert page.latest_event_id == 7
    url = http.call[0][0]
    assert "top-secret" not in url
    assert http.call[1]["headers"]["Authorization"] == "Bearer top-secret"


def test_delivery_client_preserves_feed_retry_after_without_leaking_response():
    class Response:
        status_code = 429
        headers = {"Retry-After": "17"}

        def raise_for_status(self):
            raise requests.HTTPError("private upstream body", response=self)

    class HTTP:
        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    client = DeliveryStatusClient("http://delivery:8080", "secret", http=HTTP())
    with pytest.raises(DeliveryFeedError) as caught:
        client.fetch(7)

    assert str(caught.value) == "delivery feed request failed (HTTP 429)"
    assert caught.value.retry_after == 17


def test_delivery_client_refuses_redirects_with_the_bearer_credential():
    class Response:
        status_code = 302

        @staticmethod
        def raise_for_status():
            return None

    class HTTP:
        call = None

        @classmethod
        def get(cls, *_args, **kwargs):
            cls.call = kwargs
            return Response()

    client = DeliveryStatusClient("http://delivery:8080", "secret", http=HTTP())
    with pytest.raises(DeliveryFeedError, match="HTTP 302"):
        client.fetch(7)

    assert HTTP.call["allow_redirects"] is False


def test_first_poll_baselines_then_routes_and_deduplicates():
    with tempfile.TemporaryDirectory() as tmp:
        now_box = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now_box[0],
            api=api,
        )
        feed = FakeFeed(latest=100)
        feed.events.append(delivery_event(100, 1))
        service.delivery_client = feed
        session_id = establish_chat(service, now_box[0])

        service.delivery_notifications_cycle()
        assert api.sent == []
        assert service.delivery_store.cursor() == 100

        feed.events.append(delivery_event(101, 2))
        feed.latest = 101
        service.delivery_notifications_cycle()
        assert len(api.sent) == 1
        assert api.sent[0][0:2] == ("connection", "200")
        assert "Ожидаем курьера" in api.sent[0][2]
        assert "1002" not in api.sent[0][2]
        notification = service.delivery_store.notification(101)
        assert notification["state"] == "sent"

        # A distinct authenticated source event is a new occurrence even when
        # its public status is unchanged.  This covers a rollback/reassignment
        # that happened entirely between two polls. Replaying the same page is
        # still idempotent by source_event_id.
        feed.events.append(delivery_event(102, 2))
        feed.latest = 102
        service.delivery_notifications_cycle()
        service.delivery_notifications_cycle()
        assert len(api.sent) == 2
        assert api.deleted == [("connection", (7001,))]
        with connect(service.repo.path) as db:
            assert db.execute(
                "SELECT count(*) FROM delivery_status_notifications"
            ).fetchone()[0] == 2
            cycle = db.execute(
                "SELECT first_bot_at FROM response_cycles WHERE session_id=?",
                (session_id,),
            ).fetchone()
            assert cycle["first_bot_at"] is None
            audit = db.execute(
                """SELECT template_code,cycle_id FROM business_messages
                   WHERE template_code='delivery_status_pending'"""
            ).fetchone()
            assert audit["cycle_id"] is None
        assert service.repo.bot_message_count("200", session_id, now_box[0]) == (2, 1)


@pytest.mark.parametrize(
    "language, call_to_actions",
    (
        ("ru", ("оцените нашу работу",)),
        ("uz", ("xizmatimizni baholang",)),
        ("bi", ("оцените нашу работу", "xizmatimizni baholang")),
    ),
)
def test_completed_delivery_sends_one_localized_review_link(
    language, call_to_actions
):
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=200)
        service.delivery_client = feed
        establish_chat(service, now)
        service.repo.update_language("200", language, 0.99, now)
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(201, 201, "completed"))
        feed.latest = 201
        service.delivery_notifications_cycle()

        assert len(api.sent) == 1
        sent_text = api.sent[0][2]
        for call_to_action in call_to_actions:
            assert sent_text.casefold().count(call_to_action) == 1
        assert sent_text.count(REVIEW_URL) == 1

        service.delivery_notifications_cycle()
        assert len(api.sent) == 1


def test_delivery_status_sequence_replaces_only_its_previous_bot_message():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        statuses = ("pending", "picked_up", "on_way", "completed")
        for event_id, status in enumerate(statuses, start=1):
            feed.events.append(delivery_event(event_id, 1, status))
            feed.latest = event_id
            service.delivery_notifications_cycle()

        assert api.actions == [
            ("send", 7001),
            ("send", 7002),
            ("delete", 7001),
            ("send", 7003),
            ("delete", 7002),
            ("send", 7004),
            ("delete", 7003),
        ]
        assert api.deleted == [
            ("connection", (7001,)),
            ("connection", (7002,)),
            ("connection", (7003,)),
        ]
        texts = [item[2] for item in api.sent]
        assert texts[0] == "⏳ Ожидаем курьера."
        assert texts[1] == "📦 Курьер Muzrob Oka забрал товар."
        assert "+998948765070" in texts[2]
        assert "будьте по указанному адресу" in texts[2]
        assert texts[3].count(REVIEW_URL) == 1
        assert all("1001" not in text for text in texts)

        with connect(service.repo.path) as db:
            audit = db.execute(
                """SELECT message_id,deleted_at FROM business_messages
                   WHERE sender_type='business_bot'
                     AND template_code LIKE 'delivery_status_%'
                   ORDER BY message_id"""
            ).fetchall()
            cleanup = db.execute(
                """SELECT telegram_message_id,state
                   FROM delivery_status_message_deletions
                   ORDER BY telegram_message_id"""
            ).fetchall()
        assert [row["message_id"] for row in audit] == [7001, 7002, 7003, 7004]
        assert [row["deleted_at"] is not None for row in audit] == [
            True,
            True,
            True,
            False,
        ]
        assert [tuple(row) for row in cleanup] == [
            (7001, "deleted"),
            (7002, "deleted"),
            (7003, "deleted"),
        ]


def test_same_status_courier_reassignment_sends_replacement_before_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(1, 91, "on_way"))
        feed.latest = 1
        service.delivery_notifications_cycle()

        # The source can contain only the latest transition when rollback and
        # reassignment both happened between polls. A distinct source event
        # must replace the old courier contact even without an invalidation.
        feed.events.append(
            delivery_event(
                2,
                91,
                "on_way",
                courier_id=OLMAS.user_id,
                courier_name=OLMAS.name,
                courier_phone=OLMAS.phone,
            )
        )
        feed.latest = 2
        service.delivery_notifications_cycle()

        assert api.actions == [
            ("send", 7001),
            ("send", 7002),
            ("delete", 7001),
        ]
        assert "Muzrob Oka" in api.sent[0][2]
        assert DEFAULT_COURIER.phone in api.sent[0][2]
        assert "Olmas" in api.sent[1][2]
        assert OLMAS.phone in api.sent[1][2]
        assert "Muzrob Oka" not in api.sent[1][2]
        assert DEFAULT_COURIER.phone not in api.sent[1][2]
        assert all("1091" not in text for _, _, text in api.sent)
        assert service.delivery_store.notification(1)["state"] == "sent"
        assert service.delivery_store.notification(2)["state"] == "sent"
        with connect(service.repo.path) as db:
            keys = [
                row[0]
                for row in db.execute(
                    """SELECT outbound_dedupe_key
                       FROM delivery_status_notifications
                       WHERE order_id=91 ORDER BY source_event_id"""
                )
            ]
        assert keys == [
            "delivery-status:91:on_way:1:200",
            "delivery-status:91:on_way:2:200",
        ]


def test_failed_reassignment_still_removes_old_courier_contact():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(1, 93, "on_way"))
        feed.latest = 1
        service.delivery_notifications_cycle()

        feed.events.append(
            delivery_event(
                2,
                93,
                "on_way",
                courier_id=OLMAS.user_id,
                courier_name=OLMAS.name,
                courier_phone=DEFAULT_COURIER.phone,
            )
        )
        feed.latest = 2
        service.delivery_notifications_cycle()

        assert api.actions == [("send", 7001), ("delete", 7001)]
        assert service.delivery_store.notification(2)["state"] == "failed"
        with connect(service.repo.path) as db:
            deletion = db.execute(
                """SELECT target_source_event_id,replacement_source_event_id,state
                   FROM delivery_status_message_deletions"""
            ).fetchone()
        assert tuple(deletion) == (1, 2, "deleted")


def test_invalidation_and_courier_reassignment_in_one_page_send_then_delete():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(1, 92, "on_way"))
        feed.latest = 1
        service.delivery_notifications_cycle()

        feed.invalidations.append(
            {
                "event_id": 2,
                "order_id": 92,
                "current_status": "picked_up",
                "to_status": "picked_up",
                "created_at": "2026-09-07T10:01:00+05:00",
            }
        )
        feed.events.append(
            delivery_event(
                3,
                92,
                "on_way",
                courier_id=OLMAS.user_id,
                courier_name=OLMAS.name,
                courier_phone=OLMAS.phone,
            )
        )
        feed.latest = 3
        service.delivery_notifications_cycle()

        # Replacement is sent before any customer-visible old contact is
        # deleted, including when an invalidation pre-queued the cleanup row.
        assert api.actions == [
            ("send", 7001),
            ("send", 7002),
            ("delete", 7001),
        ]
        assert "Olmas" in api.sent[-1][2]
        assert OLMAS.phone in api.sent[-1][2]
        assert "Muzrob Oka" not in api.sent[-1][2]
        assert DEFAULT_COURIER.phone not in api.sent[-1][2]
        assert service.delivery_store.notification(1)["state"] == "invalidated"
        assert service.delivery_store.notification(3)["state"] == "sent"
        with connect(service.repo.path) as db:
            deletion = db.execute(
                """SELECT target_source_event_id,replacement_source_event_id,state
                   FROM delivery_status_message_deletions"""
            ).fetchone()
        assert tuple(deletion) == (1, 3, "deleted")


@pytest.mark.parametrize(
    "courier_fields",
    (
        {"courier_id": None},
        {"courier_name": ""},
        {"courier_phone": ""},
        {"courier_phone": OLMAS.phone},
    ),
)
def test_on_way_fails_closed_without_exact_courier_contact(courier_fields):
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(1, 93, "on_way", **courier_fields))
        feed.latest = 1
        service.delivery_notifications_cycle()

        assert api.sent == []
        notification = service.delivery_store.notification(1)
        assert notification["state"] == "failed"
        assert notification["last_error"] == (
            "trusted delivery courier contact is unavailable"
        )
        with connect(service.repo.path) as db:
            assert db.execute(
                """SELECT count(*) FROM business_outbound_deliveries
                   WHERE dedupe_key LIKE 'delivery-status:%'"""
            ).fetchone()[0] == 0


def test_bilingual_on_way_message_shows_courier_phone_once():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=FakeTelegramAPI(),
        )

        text = service._render_message(
            "delivery_status_on_way",
            "bi",
            now,
            courier_id=DEFAULT_COURIER.user_id,
            courier_name=DEFAULT_COURIER.name,
            courier_phone=DEFAULT_COURIER.phone,
        )

        assert text is not None
        assert text.count("+998948765070") == 1
        assert "Курьер выехал" in text
        assert "Kuryer yo‘lga chiqdi" in text
        assert "———" in text


@pytest.mark.parametrize("language", ("ru", "uz", "bi"))
@pytest.mark.parametrize(
    "template_code, required_value",
    (
        ("delivery_status_on_way", "+998948765070"),
        ("delivery_status_completed", REVIEW_URL),
    ),
)
def test_sheet_override_cannot_omit_mandatory_delivery_detail(
    language, template_code, required_value
):
    class ShortSheetOverride:
        @staticmethod
        def render_cached(_code, selected, _values):
            return "Статус обновлён." if selected == "ru" else "Holat yangilandi."

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=FakeTelegramAPI(),
        )
        service.sheets = ShortSheetOverride()

        text = service._render_message(
            template_code,
            language,
            now,
            courier_id=DEFAULT_COURIER.user_id,
            courier_name=DEFAULT_COURIER.name,
            courier_phone=DEFAULT_COURIER.phone,
        )

        assert text is not None
        assert text.count(required_value) == 1


def test_static_sheet_courier_contact_is_replaced_by_exact_row_contact():
    class StaleStaticCourierSheetOverride:
        @staticmethod
        def render_cached(_code, selected, _values):
            if selected == "uz":
                return "Kuryer Muzrob Oka yo‘lga chiqdi. +998948765070"
            return "Курьер Muzrob Oka уже выехал. +998948765070"

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=FakeTelegramAPI(),
        )
        service.sheets = StaleStaticCourierSheetOverride()

        text = service._render_message(
            "delivery_status_on_way",
            "bi",
            now,
            courier_id=OLMAS.user_id,
            courier_name=OLMAS.name,
            courier_phone=OLMAS.phone,
        )

        assert text is not None
        assert "Muzrob Oka" not in text
        assert "+998948765070" not in text
        assert text.count("Olmas") == 1
        assert text.count("+998900979898") == 1
        assert "будьте по указанному адресу" in text
        assert "ko‘rsatilgan manzilda bo‘ling" in text


def test_static_sheet_wrong_courier_name_is_replaced_for_picked_up():
    class StaleStaticCourierSheetOverride:
        @staticmethod
        def render_cached(_code, selected, _values):
            if selected == "uz":
                return "Kuryer Muzrob Oka mahsulotni olib ketdi."
            return "Курьер Muzrob Oka забрал товар."

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=FakeTelegramAPI(),
        )
        service.sheets = StaleStaticCourierSheetOverride()

        text = service._render_message(
            "delivery_status_picked_up",
            "bi",
            now,
            courier_id=OLMAS.user_id,
            courier_name=OLMAS.name,
            courier_phone=OLMAS.phone,
        )

        assert text is not None
        assert "Muzrob Oka" not in text
        assert text.count("Olmas") == 1
        assert "забрал товар" in text
        assert "mahsulotni olib ketdi" in text


def test_sheet_override_with_extra_unknown_phone_is_replaced():
    class UnsafeCourierSheetOverride:
        @staticmethod
        def render_cached(_code, selected, values):
            name = values["courier_name"]
            phone = values["courier_phone"]
            if selected == "uz":
                return f"Kuryer {name} yo‘lga chiqdi. {phone} / +998000000000"
            return f"Курьер {name} выехал. {phone} / +998000000000"

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=FakeTelegramAPI(),
        )
        service.sheets = UnsafeCourierSheetOverride()

        text = service._render_message(
            "delivery_status_on_way",
            "bi",
            now,
            courier_id=OLMAS.user_id,
            courier_name=OLMAS.name,
            courier_phone=OLMAS.phone,
        )

        assert text is not None
        assert "+998000000000" not in text
        assert text.count(OLMAS.name) == 1
        assert text.count(OLMAS.phone) == 1


def test_cleanup_waits_for_delete_right_then_resumes_without_resending():
    with tempfile.TemporaryDirectory() as tmp:
        current = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: current[0],
            api=api,
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, current[0])
        service.delivery_notifications_cycle()

        feed.events.append(delivery_event(1, 1, "pending"))
        feed.latest = 1
        service.delivery_notifications_cycle()

        api.connection_rights = {"can_reply": True}
        service.repo.upsert_connection(
            {
                "id": "connection",
                "user": {"id": 100},
                "is_enabled": True,
                "rights": {"can_reply": True},
            },
            current[0],
        )
        feed.events.append(delivery_event(2, 1, "picked_up"))
        feed.latest = 2
        service.delivery_notifications_cycle()

        assert len(api.sent) == 2
        assert api.deleted == []
        with connect(service.repo.path) as db:
            assert db.execute(
                "SELECT state FROM delivery_status_message_deletions"
            ).fetchone()[0] == "deferred"

        current[0] += timedelta(seconds=301)
        api.connection_rights["can_delete_sent_messages"] = True
        service.repo.upsert_connection(
            {
                "id": "connection",
                "user": {"id": 100},
                "is_enabled": True,
                "rights": dict(api.connection_rights),
            },
            current[0],
        )
        service.delivery_notifications_cycle()

        assert len(api.sent) == 2
        assert api.deleted == [("connection", (7001,))]


def test_cleanup_retry_survives_restart_without_resending_status():
    class TimeoutOnceAPI(FakeTelegramAPI):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        def delete_business_messages(self, connection_id, message_ids):
            if not self.failed_once:
                self.failed_once = True
                raise TelegramAPIError(
                    "Telegram network request failed",
                    retryable=True,
                    ambiguous=True,
                )
            return super().delete_business_messages(connection_id, message_ids)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "business.db"
        current = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        first_api = TimeoutOnceAPI()
        first = BusinessService(
            business_settings(path), clock=lambda: current[0], api=first_api
        )
        feed = FakeFeed(latest=0)
        first.delivery_client = feed
        establish_chat(first, current[0])
        first.delivery_notifications_cycle()
        for event_id, status in ((1, "pending"), (2, "picked_up")):
            feed.events.append(delivery_event(event_id, 1, status))
            feed.latest = event_id
            first.delivery_notifications_cycle()

        assert len(first_api.sent) == 2
        assert first_api.deleted == []
        with connect(path) as db:
            assert db.execute(
                "SELECT state FROM delivery_status_message_deletions"
            ).fetchone()[0] == "retry"

        current[0] += timedelta(seconds=6)
        second_api = FakeTelegramAPI()
        second = BusinessService(
            business_settings(path), clock=lambda: current[0], api=second_api
        )
        second.delivery_client = feed
        second.delivery_notifications_cycle()

        assert second_api.sent == []
        assert second_api.deleted == [("connection", (7001,))]
        with connect(path) as db:
            assert db.execute(
                "SELECT state FROM delivery_status_message_deletions"
            ).fetchone()[0] == "deleted"


def test_cleanup_auth_failure_is_deferred_until_token_is_fixed():
    class UnauthorizedOnceAPI(FakeTelegramAPI):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        def delete_business_messages(self, connection_id, message_ids):
            if not self.failed_once:
                self.failed_once = True
                raise TelegramAPIError(
                    "Telegram authorization failed",
                    status=401,
                    retryable=False,
                )
            return super().delete_business_messages(connection_id, message_ids)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "business.db"
        current = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        api = UnauthorizedOnceAPI()
        service = BusinessService(
            business_settings(path), clock=lambda: current[0], api=api
        )
        feed = FakeFeed(latest=0)
        service.delivery_client = feed
        establish_chat(service, current[0])
        service.delivery_notifications_cycle()
        for event_id, status in ((1, "pending"), (2, "picked_up")):
            feed.events.append(delivery_event(event_id, 1, status))
            feed.latest = event_id
            service.delivery_notifications_cycle()

        assert len(api.sent) == 2
        assert api.deleted == []
        with connect(path) as db:
            assert db.execute(
                "SELECT state FROM delivery_status_message_deletions"
            ).fetchone()[0] == "deferred"

        current[0] += timedelta(seconds=301)
        service.delivery_notifications_cycle()

        assert len(api.sent) == 2
        assert api.deleted == [("connection", (7001,))]


def test_multiple_orders_same_chat_and_ambiguous_phone_are_safe():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=10)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        feed.events.extend((delivery_event(11, 11), delivery_event(12, 12)))
        feed.latest = 12
        service.delivery_notifications_cycle()
        assert len(api.sent) == 2

        establish_chat(service, now, chat_id="201")
        feed.events.append(delivery_event(13, 13))
        feed.latest = 13
        service.delivery_notifications_cycle()
        assert len(api.sent) == 2
        assert service.delivery_store.notification(13)["state"] == "ambiguous"


def test_expired_window_and_permanent_pause_block_but_active_order_pause_allows():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=20)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()

        service.repo.set_bot_paused("200", True, now, "active_order")
        feed.events.append(delivery_event(21, 21))
        feed.latest = 21
        service.delivery_notifications_cycle()
        assert len(api.sent) == 1

        service.repo.set_bot_paused("200", True, now, "supplier")
        feed.events.append(delivery_event(22, 22))
        feed.latest = 22
        service.delivery_notifications_cycle()
        assert len(api.sent) == 1
        assert service.delivery_store.notification(22)["state"] == "paused"

        service.repo.set_bot_paused("200", False, now)
        with connect(service.repo.path) as db:
            db.execute(
                "UPDATE business_clients SET last_client_message_at=? WHERE chat_id='200'",
                ((now - timedelta(hours=25)).isoformat(),),
            )
        feed.events.append(delivery_event(23, 23))
        feed.latest = 23
        service.delivery_notifications_cycle()
        assert len(api.sent) == 1
        assert service.delivery_store.notification(23)["state"] == "expired"


def test_manager_lock_defers_current_status_then_sends_once():
    with tempfile.TemporaryDirectory() as tmp:
        now_box = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        api = FakeTelegramAPI()
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: now_box[0],
            api=api,
        )
        feed = FakeFeed(latest=40)
        service.delivery_client = feed
        establish_chat(service, now_box[0])
        service.delivery_notifications_cycle()
        lock_until = now_box[0] + timedelta(minutes=120)
        with connect(service.repo.path) as db:
            db.execute(
                """UPDATE business_clients SET manager_lock_until=?
                   WHERE chat_id='200'""",
                (lock_until.isoformat(),),
            )
        feed.events.append(delivery_event(41, 41, "on_way"))
        feed.latest = 41

        service.delivery_notifications_cycle()
        assert api.sent == []
        assert service.delivery_store.notification(41)["state"] == "deferred"

        now_box[0] = lock_until + timedelta(seconds=1)
        service.delivery_notifications_cycle()
        assert len(api.sent) == 1
        assert service.delivery_store.notification(41)["state"] == "sent"


def test_explicit_rate_limit_retries_but_ambiguous_send_never_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        now_box = [datetime(2026, 9, 7, 20, 0, tzinfo=TZ)]
        api = RetryTelegramAPI(
            TelegramAPIError(
                "rate limited", status=429, retryable=True, retry_after=5
            )
        )
        service = BusinessService(
            business_settings(Path(tmp) / "retry.db"),
            clock=lambda: now_box[0],
            api=api,
        )
        feed = FakeFeed(latest=50)
        service.delivery_client = feed
        establish_chat(service, now_box[0])
        service.delivery_notifications_cycle()
        feed.events.append(delivery_event(51, 51))
        feed.latest = 51

        service.delivery_notifications_cycle()
        assert api.calls == 1
        assert service.delivery_store.notification(51)["state"] == "retry"
        now_box[0] += timedelta(seconds=6)
        service.delivery_notifications_cycle()
        assert api.calls == 2
        assert len(api.sent) == 1
        assert service.delivery_store.notification(51)["state"] == "sent"

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = RetryTelegramAPI(
            TelegramAPIError(
                "network outcome unknown", retryable=True, ambiguous=True
            )
        )
        service = BusinessService(
            business_settings(Path(tmp) / "uncertain.db"),
            clock=lambda: now,
            api=api,
        )
        feed = FakeFeed(latest=60)
        service.delivery_client = feed
        establish_chat(service, now)
        service.delivery_notifications_cycle()
        feed.events.append(delivery_event(61, 61))
        feed.latest = 61

        service.delivery_notifications_cycle()
        service.delivery_notifications_cycle()
        assert api.calls == 1
        assert api.sent == []
        assert service.delivery_store.notification(61)["state"] == "uncertain"


def test_delivery_store_survives_restart_without_resending():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "business.db"
        now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
        api = FakeTelegramAPI()
        feed = FakeFeed(latest=30)
        first = BusinessService(business_settings(path), clock=lambda: now, api=api)
        first.delivery_client = feed
        establish_chat(first, now)
        first.delivery_notifications_cycle()
        feed.events.append(delivery_event(31, 31))
        feed.latest = 31
        first.delivery_notifications_cycle()
        assert len(api.sent) == 1

        second = BusinessService(business_settings(path), clock=lambda: now, api=api)
        second.delivery_client = feed
        second.delivery_notifications_cycle()
        assert second.delivery_store.cursor() == 31
        assert len(api.sent) == 1


def test_import_recovers_stale_running_before_newer_status_supersedes_it(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    assert store.reconcile_feed(FEED_ID, 0, False, now) is False
    assert store.import_page(
        0,
        1,
        [delivery_event(1, 70, "on_way")],
        now,
        feed_instance_id=FEED_ID,
    ) == 1
    running = store.claim_due(now, limit=1, lease_seconds=60)[0]
    assert running["state"] == "running"

    recovered_at = now + timedelta(seconds=61)
    assert store.import_page(
        1,
        2,
        [delivery_event(2, 70, "completed")],
        recovered_at,
        feed_instance_id=FEED_ID,
    ) == 1

    assert store.notification(1)["state"] == "superseded"
    due = store.claim_due(recovered_at, limit=10)
    assert [row["source_event_id"] for row in due] == [2]


def test_hidden_rollback_invalidation_supersedes_deferred_status(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    store.reconcile_feed(FEED_ID, 0, False, now)
    store.import_page(
        0,
        1,
        [delivery_event(1, 71, "completed")],
        now,
        feed_instance_id=FEED_ID,
    )
    claimed = store.claim_due(now, limit=1)[0]
    assert store.finish(
        1,
        claimed["lease_token"],
        now,
        "deferred",
        retry_after=300,
    )

    store.import_page(
        1,
        2,
        [],
        now + timedelta(seconds=1),
        invalidations=[{
            "event_id": 2,
            "order_id": 71,
            "current_status": "picked_up",
            "to_status": "picked_up",
        }],
        feed_instance_id=FEED_ID,
    )

    assert store.notification(1)["state"] == "superseded"
    assert store.claim_due(now + timedelta(minutes=10), limit=10) == []


def test_repeated_public_status_keeps_event_history_and_replaces_unsent_row(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    store.reconcile_feed(FEED_ID, 0, False, now)
    store.import_page(
        0,
        1,
        [delivery_event(1, 72, "on_way")],
        now,
        feed_instance_id=FEED_ID,
    )
    first = store.claim_due(now, limit=1)[0]
    store.finish(
        1,
        first["lease_token"],
        now,
        "deferred",
        retry_after=300,
    )
    store.import_page(
        1,
        2,
        [],
        now + timedelta(seconds=1),
        invalidations=[{
            "event_id": 2,
            "order_id": 72,
            "current_status": "picked_up",
            "to_status": "picked_up",
        }],
        feed_instance_id=FEED_ID,
    )
    assert store.import_page(
        2,
        3,
        [delivery_event(3, 72, "on_way")],
        now + timedelta(seconds=2),
        feed_instance_id=FEED_ID,
    ) == 1

    assert store.notification(1)["state"] == "superseded"
    replacement = store.notification(3)
    assert replacement["state"] == "pending"
    with connect(path) as db:
        assert db.execute(
            """SELECT count(*) FROM delivery_status_notifications
               WHERE order_id=72 AND public_status='on_way'"""
        ).fetchone()[0] == 2


def test_feed_instance_change_and_cursor_reset_rebaseline_without_history(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    store.reconcile_feed(FEED_ID, 10, False, now)
    store.import_page(
        10,
        11,
        [delivery_event(11, 73, "pending")],
        now,
        feed_instance_id=FEED_ID,
    )
    replacement_id = "22222222-2222-4222-8222-222222222222"
    page = validate_feed_page(
        {
            "feed_instance_id": replacement_id,
            "events": [],
            "invalidations": [],
            "next_after_event_id": 11,
            "latest_event_id": 3,
            "has_more": False,
            "cursor_reset_required": True,
        },
        11,
    )

    assert store.reconcile_feed(
        page.feed_instance_id,
        page.latest_event_id,
        page.cursor_reset_required,
        now + timedelta(seconds=1),
    ) is False
    assert store.cursor() == 3
    assert store.feed_instance_id() == replacement_id
    with connect(path) as db:
        assert db.execute(
            "SELECT count(*) FROM delivery_status_notifications"
        ).fetchone()[0] == 0


def test_same_feed_identity_reset_clears_reusable_event_and_dedupe_ids(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    store.reconcile_feed(FEED_ID, 10, False, now)
    store.import_page(
        10,
        11,
        [delivery_event(11, 73, "pending")],
        now,
        feed_instance_id=FEED_ID,
    )
    with connect(path) as db:
        db.execute(
            """INSERT INTO business_outbound_deliveries(
               dedupe_key,chat_id,session_id,template_code,content_hash,
               state,created_at,updated_at)
               VALUES(?,?,?,?,?,'sent',?,?)""",
            (
                "delivery-status:73:pending:200",
                "200",
                "session",
                "delivery_status_pending",
                "hash",
                now.isoformat(),
                now.isoformat(),
            ),
        )

    assert store.reconcile_feed(FEED_ID, 3, True, now) is False
    assert store.cursor() == 3
    assert store.import_page(
        3,
        11,
        [delivery_event(11, 99, "pending")],
        now + timedelta(seconds=1),
        feed_instance_id=FEED_ID,
    ) == 1
    assert store.notification(11)["order_id"] == 99
    with connect(path) as db:
        assert db.execute(
            """SELECT count(*) FROM business_outbound_deliveries
               WHERE dedupe_key LIKE 'delivery-status:%'"""
        ).fetchone()[0] == 0


def test_legacy_cursor_without_feed_identity_is_safely_rebaselined(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    assert store.initialize_cursor(7, now)

    assert store.reconcile_feed(FEED_ID, 20, False, now) is False
    assert store.cursor() == 20
    assert store.feed_instance_id() == FEED_ID


def test_global_telegram_not_before_only_moves_forward(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    store = DeliveryNotificationStore(path)
    now = datetime(2026, 9, 7, 20, 0, tzinfo=TZ)
    later = now + timedelta(seconds=30)
    assert store.set_telegram_not_before(later, now) == later
    assert store.set_telegram_not_before(now + timedelta(seconds=5), now) == later
    assert store.telegram_not_before() == later


def test_delivery_notification_tables_are_additive_and_keep_phone_private():
    with tempfile.TemporaryDirectory() as tmp:
        service = BusinessService(
            business_settings(Path(tmp) / "business.db"),
            clock=lambda: datetime(2026, 9, 7, 20, 0, tzinfo=TZ),
            api=FakeTelegramAPI(),
        )
        with connect(service.repo.path) as db:
            tables = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {
                "business_chat_phones",
                "business_integration_state",
                "delivery_status_notifications",
            } <= tables
            assert not db.execute(
                """SELECT 1 FROM sheets_outbox
                   WHERE payload LIKE '%+998901112233%'"""
            ).fetchone()
