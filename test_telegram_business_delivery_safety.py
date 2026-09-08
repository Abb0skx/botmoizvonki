from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.utils.couriers import COURIERS_BY_ID
from telegram_business.config import BusinessSettings
from telegram_business.delivery_notifications import DeliveryFeedPage
from telegram_business.migrations import connect
from telegram_business.request_inputs import phones_from_message
from telegram_business.service import BusinessService
from telegram_business.telegram_api import TelegramAPIError


TZ = ZoneInfo("Asia/Tashkent")
FEED_INSTANCE_ID = str(UUID("8fe74570-c699-4f2e-b410-1f35debf7961"))
PHONE = "+998901112233"
DEFAULT_COURIER = COURIERS_BY_ID[1799690992]


class RecordingTelegramAPI:
    def __init__(self, errors: list[Exception] | None = None):
        self.errors = list(errors or ())
        self.calls: list[tuple[str, str, str]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, tuple[int, ...]]] = []

    def send_message(self, connection_id, chat_id, text, **_options):
        call = (str(connection_id), str(chat_id), str(text))
        self.calls.append(call)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(call)
        return {
            "ok": True,
            "result": {"message_id": 8000 + len(self.sent)},
        }

    def get_business_connection(self, connection_id):
        return {
            "id": connection_id,
            "user": {"id": 100},
            "is_enabled": True,
            "rights": {
                "can_reply": True,
                "can_delete_sent_messages": True,
            },
        }

    def delete_business_messages(self, connection_id, message_ids):
        self.deleted.append((str(connection_id), tuple(message_ids)))
        return {"ok": True, "result": True}


class CaughtUpFeed:
    """A valid feed that has no events after the test's seeded cursor."""

    def __init__(self, latest_event_id: int):
        self.latest_event_id = int(latest_event_id)
        self.calls: list[tuple[int, int]] = []

    def fetch(self, after_event_id, limit=100):
        after = int(after_event_id)
        self.calls.append((after, int(limit)))
        assert after == self.latest_event_id
        return DeliveryFeedPage(
            events=(),
            next_after_event_id=after,
            latest_event_id=self.latest_event_id,
            has_more=False,
            invalidations=(),
            feed_instance_id=FEED_INSTANCE_ID,
            cursor_reset_required=False,
        )


class TwoPageFeed:
    def __init__(self, first: dict, second: dict):
        self.first = first
        self.second = second

    def fetch(self, after_event_id, limit=100):
        if int(after_event_id) == 0:
            return DeliveryFeedPage(
                events=(self.first,),
                next_after_event_id=int(self.first["event_id"]),
                latest_event_id=int(self.second["event_id"]),
                has_more=True,
                feed_instance_id=FEED_INSTANCE_ID,
            )
        assert int(after_event_id) == int(self.first["event_id"])
        return DeliveryFeedPage(
            events=(self.second,),
            next_after_event_id=int(self.second["event_id"]),
            latest_event_id=int(self.second["event_id"]),
            has_more=False,
            feed_instance_id=FEED_INSTANCE_ID,
        )


class ElevenPageBacklogFeed:
    """Expose 2,001 raw rows so one worker cycle cannot catch up."""

    def __init__(self, first: dict, last: dict):
        self.latest_event_id = int(last["event_id"])
        self.events = {
            int(first["event_id"]): first,
            int(last["event_id"]): last,
        }
        self.invalidations = {
            event_id: {
                "event_id": event_id,
                "order_id": 10_000 + event_id,
                "current_status": "draft",
                "to_status": "draft",
            }
            for event_id in range(2, self.latest_event_id)
        }
        self.calls: list[tuple[int, int]] = []

    def fetch(self, after_event_id, limit=100):
        after = int(after_event_id)
        bounded = int(limit)
        self.calls.append((after, bounded))
        event_ids = list(
            range(
                after + 1,
                min(self.latest_event_id, after + bounded) + 1,
            )
        )
        next_id = event_ids[-1] if event_ids else after
        return DeliveryFeedPage(
            events=tuple(
                self.events[event_id]
                for event_id in event_ids
                if event_id in self.events
            ),
            next_after_event_id=next_id,
            latest_event_id=self.latest_event_id,
            has_more=next_id < self.latest_event_id,
            invalidations=tuple(
                self.invalidations[event_id]
                for event_id in event_ids
                if event_id in self.invalidations
            ),
            feed_instance_id=FEED_INSTANCE_ID,
        )


def settings(path: Path, *, max_messages_10m: int = 4) -> BusinessSettings:
    return BusinessSettings(
        enabled=True,
        bot_token="123:token",
        webhook_secret="safe_secret",
        allowed_connection_id="connection",
        admin_chat_id="",
        timezone="Asia/Tashkent",
        night_start=time(20),
        night_end=time(9, 30),
        manager_start=time(10),
        manager_end=time(20),
        final_idle_seconds=300,
        debounce_seconds=3,
        manager_lock_minutes=120,
        credit_cooldown_minutes=720,
        max_messages_10m=max_messages_10m,
        max_messages_session=8,
        db_path=path,
        sheet_id="sheet",
        sheets_sync_seconds=60,
        template_cache_seconds=300,
        product_source="existing_google_bot_prices",
        product_db_path="",
        product_price_max_age_minutes=1440,
        bot_id="123",
        delivery_notifications_enabled=True,
        delivery_notifications_url="http://delivery-stats:8080",
        delivery_notifications_token="internal-secret",
        delivery_notifications_poll_seconds=5,
        delivery_notifications_max_event_age_hours=24,
    )


def establish_chat(
    service: BusinessService,
    now: datetime,
    *,
    chat_id: str = "200",
    phone: str = PHONE,
    message_id: int = 10,
) -> str:
    incoming_at = now - timedelta(minutes=5)
    service.repo.upsert_connection(
        {
            "id": "connection",
            "user": {"id": 100},
            "is_enabled": True,
            "rights": {
                "can_reply": True,
                "can_delete_sent_messages": True,
            },
        },
        now,
    )
    service.repo.upsert_client(chat_id, {"id": int(chat_id)}, now)
    session = service.repo.session(chat_id, incoming_at)
    message = {
        "business_connection_id": "connection",
        "message_id": message_id,
        "date": int(incoming_at.timestamp()),
        "chat": {"id": int(chat_id), "type": "private"},
        "from": {"id": int(chat_id), "language_code": "ru"},
        "text": f"мой номер {phone}",
    }
    assert service.repo.save_message(
        "connection",
        message,
        session["session_id"],
        "client",
        now,
        update_id=message_id,
    )
    service.repo.touch_client_message(
        chat_id,
        session["session_id"],
        now,
        event_at=incoming_at,
        message_id=message_id,
    )
    service.repo.replace_client_message_phones(
        "connection",
        chat_id,
        message_id,
        phones_from_message(message, message["text"]),
        incoming_at,
        telegram_user_id=chat_id,
    )
    service.repo.update_language(chat_id, "ru", 0.99, now)
    service.repo.record_bot_message(
        "connection",
        chat_id,
        session["session_id"],
        9000 + int(chat_id),
        "Здравствуйте",
        "greeting_model",
        incoming_at + timedelta(seconds=1),
        count_as_response=False,
    )
    return str(session["session_id"])


def delivery_event(
    event_id: int,
    order_id: int,
    created_at: datetime,
    *,
    status: str = "pending",
    phone: str = PHONE,
) -> dict:
    return {
        "event_id": int(event_id),
        "order_id": int(order_id),
        "order_number": str(1000 + int(order_id)),
        "status": status,
        "public_status": status,
        "current_status": status,
        "from_status": "draft" if status == "pending" else "pending",
        "product": "iPhone 16 Pro Max",
        "phones": (phone,),
        "client_phone": phone,
        "client_phone_2": "",
        "courier_id": DEFAULT_COURIER.user_id,
        "courier_name": DEFAULT_COURIER.name,
        "courier_phone": DEFAULT_COURIER.phone,
        "created_at": created_at.isoformat(),
    }


def seed_notifications(
    service: BusinessService,
    now: datetime,
    events: list[dict],
) -> None:
    assert service.delivery_store.reconcile_feed(
        FEED_INSTANCE_ID,
        0,
        False,
        now,
    ) is False
    last_event_id = max(int(event["event_id"]) for event in events)
    assert service.delivery_store.import_page(
        0,
        last_event_id,
        events,
        now,
        feed_instance_id=FEED_INSTANCE_ID,
    ) == len(events)
    service.delivery_client = CaughtUpFeed(last_event_id)


def test_unmatched_notification_is_deferred_then_sent_after_phone_appears(tmp_path):
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: current[0],
        api=api,
    )
    seed_notifications(
        service,
        current[0],
        [delivery_event(1, 1, current[0])],
    )

    service.delivery_notifications_cycle()

    first = service.delivery_store.notification(1)
    assert first["state"] == "deferred"
    assert first["match_outcome"] == "deferred"
    assert api.calls == []

    current[0] += timedelta(seconds=61)
    establish_chat(service, current[0])
    service.delivery_notifications_cycle()

    assert len(api.sent) == 1
    assert api.sent[0][0:2] == ("connection", "200")
    assert service.delivery_store.notification(1)["state"] == "sent"


def test_multi_page_backlog_sends_only_newest_status_after_catching_up(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    service.delivery_store.reconcile_feed(FEED_INSTANCE_ID, 0, False, now)
    service.delivery_client = TwoPageFeed(
        delivery_event(1, 1, now, status="pending"),
        delivery_event(2, 1, now, status="completed"),
    )

    service.delivery_notifications_cycle()

    assert len(api.sent) == 1
    assert "доставлен" in api.sent[0][2]
    assert service.delivery_store.notification(1)["state"] == "superseded"
    assert service.delivery_store.notification(2)["state"] == "sent"


def test_backlog_beyond_ten_page_cap_sends_nothing_until_fully_caught_up(
    tmp_path,
):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    service.delivery_store.reconcile_feed(FEED_INSTANCE_ID, 0, False, now)
    feed = ElevenPageBacklogFeed(
        delivery_event(1, 1, now, status="pending"),
        delivery_event(2001, 1, now, status="completed"),
    )
    service.delivery_client = feed

    service.delivery_notifications_cycle()

    assert len(feed.calls) == 10
    assert service.delivery_store.cursor() == 2000
    assert api.calls == []
    assert service.delivery_store.notification(1)["state"] == "pending"

    service.delivery_notifications_cycle()

    assert len(feed.calls) == 11
    assert service.delivery_store.cursor() == 2001
    assert len(api.sent) == 1
    assert "доставлен" in api.sent[0][2]
    assert service.delivery_store.notification(1)["state"] == "superseded"
    assert service.delivery_store.notification(2001)["state"] == "sent"


def test_send_time_recheck_stops_concurrent_phone_deletion(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "delete_phone.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    seed_notifications(service, now, [delivery_event(1, 1, now)])
    original_render = service._render_message
    mutated = False

    def render_with_race(*args, **kwargs):
        nonlocal mutated
        text = original_render(*args, **kwargs)
        if not mutated:
            mutated = True
            service.repo.mark_deleted_messages(
                "connection", "200", [10], now
            )
        return text

    service._render_message = render_with_race
    service.delivery_notifications_cycle()

    assert mutated is True
    assert api.calls == []
    assert service.delivery_store.notification(1)["state"] == "deferred"


def test_concurrent_manager_fence_does_not_block_delivery_status(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "manager_fence.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    seed_notifications(service, now, [delivery_event(1, 1, now)])
    original_render = service._render_message
    manager_update_saved = False

    def render_with_manager_reply(*args, **kwargs):
        nonlocal manager_update_saved
        text = original_render(*args, **kwargs)
        if not manager_update_saved:
            manager_update_saved = True
            manager_update = {
                "update_id": 700,
                "business_message": {
                    "business_connection_id": "connection",
                    "message_id": 701,
                    "date": int(now.timestamp()),
                    "chat": {"id": 200, "type": "private"},
                    "from": {"id": 100},
                    "text": "Ответ менеджера",
                },
            }
            assert service.repo.save_update(
                manager_update,
                now,
                allowed_connection_id="connection",
            )
        return text

    service._render_message = render_with_manager_reply
    service.delivery_notifications_cycle()

    assert manager_update_saved is True
    assert len(api.sent) == 1
    assert service.delivery_store.notification(1)["state"] == "sent"


def test_event_older_than_configured_max_age_is_not_sent(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    seed_notifications(
        service,
        now,
        [delivery_event(1, 1, now - timedelta(hours=24, seconds=1))],
    )

    service.delivery_notifications_cycle()

    assert api.calls == []
    assert service.delivery_store.notification(1)["state"] == "expired"


def test_business_reply_window_includes_exact_24h_and_rejects_one_second_more(
    tmp_path,
):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    with connect(service.repo.path) as db:
        db.execute(
            "UPDATE business_clients SET last_client_message_at=? WHERE chat_id='200'",
            ((now - timedelta(hours=24)).isoformat(),),
        )
    seed_notifications(service, now, [delivery_event(1, 1, now)])
    service.delivery_notifications_cycle()
    assert len(api.sent) == 1

    with connect(service.repo.path) as db:
        db.execute(
            "UPDATE business_clients SET last_client_message_at=? WHERE chat_id='200'",
            ((now - timedelta(hours=24, seconds=1)).isoformat(),),
        )
    assert service.delivery_store.import_page(
        1,
        2,
        [delivery_event(2, 2, now)],
        now,
        feed_instance_id=FEED_INSTANCE_ID,
    ) == 1
    service.delivery_client = CaughtUpFeed(2)
    service.delivery_notifications_cycle()
    assert len(api.sent) == 1
    assert service.delivery_store.notification(2)["state"] == "expired"


def test_delivery_order_never_migrates_to_another_chat_after_first_send(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now, chat_id="200", message_id=10)
    seed_notifications(service, now, [delivery_event(1, 1, now)])
    service.delivery_notifications_cycle()
    assert len(api.sent) == 1

    service.repo.mark_deleted_messages("connection", "200", [10], now)
    establish_chat(service, now, chat_id="201", message_id=20)
    assert service.delivery_store.import_page(
        1,
        2,
        [delivery_event(2, 1, now, status="on_way")],
        now,
        feed_instance_id=FEED_INSTANCE_ID,
    ) == 1
    service.delivery_client = CaughtUpFeed(2)

    service.delivery_notifications_cycle()

    assert len(api.sent) == 1
    assert service.delivery_store.notification(2)["state"] == "ambiguous"


def test_all_bot_messages_share_four_per_chat_per_ten_minute_throttle(
    tmp_path,
):
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "business.db", max_messages_10m=4),
        clock=lambda: current[0],
        api=api,
    )
    establish_chat(service, current[0])
    seed_notifications(
        service,
        current[0],
        [delivery_event(index, index, current[0]) for index in range(1, 6)],
    )

    service.delivery_notifications_cycle()

    # The recent greeting also consumes one slot in the global bot-message
    # budget, so only three transactional updates can be sent immediately.
    assert len(api.sent) == 3
    assert [service.delivery_store.notification(index)["state"] for index in range(1, 4)] == [
        "sent",
        "sent",
        "sent",
    ]
    assert service.delivery_store.notification(4)["state"] == "deferred"
    assert service.delivery_store.notification(5)["state"] == "deferred"

    current[0] += timedelta(seconds=601)
    service.delivery_notifications_cycle()
    assert len(api.sent) == 5
    assert service.delivery_store.notification(4)["state"] == "sent"
    assert service.delivery_store.notification(5)["state"] == "sent"


def test_telegram_429_blocks_following_rows_and_survives_restart(tmp_path):
    path = tmp_path / "business.db"
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    retry_after = 120
    first_api = RecordingTelegramAPI(
        [
            TelegramAPIError(
                "rate limited",
                status=429,
                retryable=True,
                retry_after=retry_after,
            )
        ]
    )
    first = BusinessService(
        settings(path),
        clock=lambda: current[0],
        api=first_api,
    )
    establish_chat(first, current[0])
    seed_notifications(
        first,
        current[0],
        [delivery_event(1, 1, current[0]), delivery_event(2, 2, current[0])],
    )

    first.delivery_notifications_cycle()

    assert len(first_api.calls) == 1
    assert first.delivery_store.notification(1)["state"] == "retry"
    assert first.delivery_store.notification(2)["state"] == "pending"
    assert first.delivery_store.telegram_not_before() == current[0] + timedelta(
        seconds=retry_after
    )

    current[0] += timedelta(seconds=60)
    second_api = RecordingTelegramAPI()
    second = BusinessService(
        settings(path),
        clock=lambda: current[0],
        api=second_api,
    )
    second.delivery_client = CaughtUpFeed(2)
    second.delivery_notifications_cycle()
    assert second_api.calls == []

    current[0] += timedelta(seconds=61)
    second.delivery_notifications_cycle()
    assert len(second_api.sent) == 2
    assert second.delivery_store.notification(1)["state"] == "sent"
    assert second.delivery_store.notification(2)["state"] == "sent"


def test_crash_after_telegram_accepts_message_never_resends_on_restart(tmp_path):
    path = tmp_path / "business.db"
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    first_api = RecordingTelegramAPI()
    first = BusinessService(settings(path), clock=lambda: current[0], api=first_api)
    establish_chat(first, current[0])
    seed_notifications(first, current[0], [delivery_event(1, 1, current[0])])
    real_finish = first.delivery_store.finish_sent_and_queue_cleanup
    crashed = False

    def crash_before_notification_finish(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after Telegram acceptance")
        return real_finish(*args, **kwargs)

    first.delivery_store.finish_sent_and_queue_cleanup = (
        crash_before_notification_finish
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        first.delivery_notifications_cycle()
    assert crashed is True
    assert len(first_api.sent) == 1
    assert first.delivery_store.notification(1)["state"] == "running"

    current[0] += timedelta(seconds=61)
    second_api = RecordingTelegramAPI()
    second = BusinessService(settings(path), clock=lambda: current[0], api=second_api)
    second.delivery_client = CaughtUpFeed(1)
    second.delivery_notifications_cycle()

    assert second_api.calls == []
    assert second.delivery_store.notification(1)["state"] == "sent"


@pytest.mark.parametrize(
    "remap_phone, expected_state",
    ((False, "uncertain"), (True, "ambiguous")),
)
def test_persisted_sending_ledger_never_retries_after_lease_expiry(
    tmp_path,
    remap_phone,
    expected_state,
):
    path = tmp_path / f"sending-{int(remap_phone)}.db"
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    first = BusinessService(
        settings(path),
        clock=lambda: current[0],
        api=RecordingTelegramAPI(),
    )
    session_id = establish_chat(first, current[0], chat_id="200", message_id=10)
    seed_notifications(first, current[0], [delivery_event(1, 1, current[0])])
    claimed = first.delivery_store.claim_due(
        current[0], limit=1, lease_seconds=60
    )[0]
    assert claimed["state"] == "running"
    assert first.repo.begin_outbound_delivery(
        "delivery-status:1:pending:200",
        "200",
        session_id,
        "delivery_status_pending",
        "simulated-content-hash",
        current[0],
        business_connection_id="connection",
    ) == "send"

    if remap_phone:
        first.repo.mark_deleted_messages(
            "connection", "200", [10], current[0] + timedelta(seconds=1)
        )
        establish_chat(
            first,
            current[0] + timedelta(seconds=2),
            chat_id="201",
            message_id=20,
        )

    current[0] += timedelta(seconds=61)
    second_api = RecordingTelegramAPI()
    second = BusinessService(
        settings(path),
        clock=lambda: current[0],
        api=second_api,
    )
    second.delivery_client = CaughtUpFeed(1)

    second.delivery_notifications_cycle()

    assert second_api.calls == []
    assert second.delivery_store.notification(1)["state"] == expected_state
    with connect(path) as db:
        ledgers = db.execute(
            """SELECT state,chat_id,business_connection_id
               FROM business_outbound_deliveries"""
        ).fetchall()
    assert [
        (row["state"], row["chat_id"], row["business_connection_id"])
        for row in ledgers
    ] == [
        ("sending", "200", "connection")
    ]


@pytest.mark.parametrize(
    "language, expected",
    (("ru", "Ожидаем курьера"), ("uz", "Kuryerni kutyapmiz"), ("bi", "———")),
)
def test_delivery_status_uses_saved_client_language(tmp_path, language, expected):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / f"{language}.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    service.repo.update_language("200", language, 0.99, now)
    seed_notifications(service, now, [delivery_event(1, 1, now)])

    service.delivery_notifications_cycle()

    assert len(api.sent) == 1
    assert expected in api.sent[0][2]


def test_delivery_rechecks_language_immediately_before_send(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "language_race.db"),
        clock=lambda: now,
        api=api,
    )
    establish_chat(service, now)
    service.repo.update_language("200", "uz", 0.69, now)
    seed_notifications(service, now, [delivery_event(1, 1, now)])
    original_render = service._render_message
    switched = False

    def render_while_language_changes(*args, **kwargs):
        nonlocal switched
        text = original_render(*args, **kwargs)
        if not switched:
            switched = True
            service.repo.update_language("200", "ru", 0.80, now)
        return text

    service._render_message = render_while_language_changes
    service.delivery_notifications_cycle()

    assert switched is True
    assert len(api.sent) == 1
    assert "Ожидаем курьера" in api.sent[0][2]
    assert "Kuryerni kutyapmiz" not in api.sent[0][2]


def test_delivery_uses_explicit_language_seen_during_manager_lock(tmp_path):
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    api = RecordingTelegramAPI()
    service = BusinessService(
        settings(tmp_path / "language_refresh.db"),
        clock=lambda: current[0],
        api=api,
    )
    establish_chat(service, current[0])
    service.repo.update_language("200", "uz", 0.69, current[0])

    manager_update = {
        "update_id": 700,
        "business_message": {
            "business_connection_id": "connection",
            "message_id": 701,
            "date": int(current[0].timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 100},
            "text": "Ответ менеджера",
        },
    }
    assert service.repo.save_update(
        manager_update,
        current[0],
        allowed_connection_id="connection",
    )
    service.process_update(manager_update)

    current[0] += timedelta(seconds=1)
    client_update = {
        "update_id": 702,
        "business_message": {
            "business_connection_id": "connection",
            "message_id": 703,
            "date": int(current[0].timestamp()),
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "language_code": "uz"},
            "text": "Какая цена и сколько стоит?",
        },
    }
    assert service.repo.save_update(
        client_update,
        current[0],
        allowed_connection_id="connection",
    )
    service.process_update(client_update)
    assert service.repo.client("200")["language"] == "ru"
    assert service.repo.due_actions(current[0]) == []

    seed_notifications(
        service,
        current[0],
        [delivery_event(1, 1, current[0])],
    )
    service.delivery_notifications_cycle()

    assert len(api.sent) == 1
    assert "Ожидаем курьера" in api.sent[0][2]
    assert "Kuryerni kutyapmiz" not in api.sent[0][2]
