import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram_business.config import BusinessSettings
from telegram_business.migrations import connect
from telegram_business.repository import BusinessRepository
from telegram_business.request_inputs import mask_recognized_phones, phones_from_message
from telegram_business.service import BusinessService


TZ = ZoneInfo("Asia/Tashkent")


def settings(path: Path) -> BusinessSettings:
    base = BusinessSettings(
        False, "999:token", "secret", "connection", "", "Asia/Tashkent",
        time(20), time(9, 30), time(10), time(20), 300, 3, 120, 720, 4, 8,
        path, "sheet", 60, 300, "existing_google_bot_prices", "", 1440,
    )
    return replace(base, bot_id="999")


def save_client(
    repo: BusinessRepository,
    connection_id: str,
    chat_id: str,
    message_id: int,
    text: str,
    now: datetime,
) -> str:
    session = repo.session(chat_id, now, time(20), time(9, 30))
    repo.upsert_client(chat_id, {"id": int(chat_id)}, now)
    message = {
        "business_connection_id": connection_id,
        "message_id": message_id,
        "date": int(now.timestamp()),
        "chat": {"id": int(chat_id), "type": "private"},
        "from": {"id": int(chat_id)},
        "text": text,
    }
    assert repo.save_message(
        connection_id, message, session["session_id"], "client", now,
    )
    repo.touch_client_message(
        chat_id, session["session_id"], now,
        event_at=now, message_id=message_id,
    )
    repo.replace_client_message_phones(
        connection_id, chat_id, message_id,
        phones_from_message(message, text), now,
        telegram_user_id=chat_id,
    )
    return session["session_id"]


def save_outbound(
    repo: BusinessRepository,
    connection_id: str,
    chat_id: str,
    session_id: str,
    message_id: int,
    now: datetime,
    sender_type: str = "manager",
) -> None:
    message = {
        "business_connection_id": connection_id,
        "message_id": message_id,
        "date": int(now.timestamp()),
        "chat": {"id": int(chat_id), "type": "private"},
        "from": {"id": 100},
        "text": "outbound",
    }
    assert repo.save_message(
        connection_id, message, session_id, sender_type, now,
    )


def test_phone_table_is_additive_and_unique_match_requires_prior_outbound(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    repo = BusinessRepository(tmp_path / "business.db")
    repo.upsert_connection(
        {"id": "connection", "user": {"id": 100},
         "is_enabled": True, "rights": {"can_reply": True}},
        now,
    )
    session_id = save_client(
        repo, "connection", "200", 1,
        "Телефоны +998 90 123 45 67 и +998 93 765 43 21", now,
    )

    assert repo.match_delivery_phones(["+998901234567"], now) == {
        "outcome": "unmatched",
    }
    save_outbound(repo, "connection", "200", session_id, 2, now)
    assert repo.match_delivery_phones(
        ["90 123 45 67", "+998937654321"], now,
    ) == {
        "outcome": "unique",
        "connection_id": "connection",
        "chat_id": "200",
    }
    with connect(repo.path) as db:
        columns = {
            row["name"] for row in db.execute(
                "PRAGMA table_info(business_chat_phones)"
            ).fetchall()
        }
        assert {"phone_normalized", "source_message_id", "deleted_at"} <= columns
        assert db.execute(
            "SELECT COUNT(*) FROM business_chat_phones WHERE deleted_at IS NULL"
        ).fetchone()[0] == 2


def test_message_sheet_payload_masks_phones_but_sqlite_keeps_private_text(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    repo = BusinessRepository(tmp_path / "business.db")
    repo.upsert_connection(
        {"id": "connection", "user": {"id": 100},
         "is_enabled": True, "rights": {"can_reply": True}},
        now,
    )
    text = "Мой номер +998 90 123 45 67, второй +998 93 765 43 21"
    save_client(repo, "connection", "200", 1, text, now)

    with connect(repo.path) as db:
        stored = db.execute(
            "SELECT text FROM business_messages WHERE message_id=1"
        ).fetchone()[0]
        exported = json.loads(db.execute(
            """SELECT payload FROM sheets_outbox
               WHERE entity_type='message'"""
        ).fetchone()[0])["text"]

    assert stored == text
    assert "+998 90 123 45 67" not in exported
    assert "+998 93 765 43 21" not in exported
    assert exported.count("+998 ** *** **") == 2
    assert exported.endswith("21")

    labelled = "telefon 90 123 45 67 yoki 93 765 43 21"
    assert "67 yoki" in (mask_recognized_phones(labelled) or "")


def test_matching_ignores_disabled_or_non_replyable_old_connections(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    repo = BusinessRepository(tmp_path / "business.db")
    for connection_id in ("old-disabled", "old-no-reply", "current"):
        repo.upsert_connection(
            {"id": connection_id, "user": {"id": 100},
             "is_enabled": True, "rights": {"can_reply": True}},
            now,
        )
        session_id = save_client(
            repo, connection_id, "200", 1, "+998 90 123 45 67", now,
        )
        save_outbound(repo, connection_id, "200", session_id, 2, now)

    repo.upsert_connection(
        {"id": "old-disabled", "user": {"id": 100},
         "is_enabled": False, "rights": {"can_reply": True}},
        now + timedelta(seconds=1),
    )
    repo.upsert_connection(
        {"id": "old-no-reply", "user": {"id": 100},
         "is_enabled": True, "rights": {"can_reply": False}},
        now + timedelta(seconds=1),
    )

    assert repo.match_delivery_phones(["+998901234567"], now) == {
        "outcome": "unique",
        "connection_id": "current",
        "chat_id": "200",
    }


def test_same_phone_or_multiple_order_phones_never_guess_between_chats(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    repo = BusinessRepository(tmp_path / "business.db")
    repo.upsert_connection(
        {"id": "connection", "user": {"id": 100},
         "is_enabled": True, "rights": {"can_reply": True}},
        now,
    )
    for chat_id, phone, offset in (
        ("200", "+998901234567", 0),
        ("201", "+998901234567", 10),
        ("202", "+998937654321", 20),
    ):
        session_id = save_client(
            repo, "connection", chat_id, 1 + offset, phone, now,
        )
        save_outbound(
            repo, "connection", chat_id, session_id, 2 + offset, now,
            sender_type="business_bot",
        )

    assert repo.match_delivery_phones(["+998901234567"], now) == {
        "outcome": "ambiguous",
    }
    assert repo.match_delivery_phones(
        ["+998901234567", "+998937654321"], now,
    ) == {"outcome": "ambiguous"}
    assert repo.match_delivery_phones(["8600123456789012"], now) == {
        "outcome": "unmatched",
    }


def test_bounded_backfill_is_idempotent_and_recovers_an_interrupted_edit(tmp_path):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)
    repo = BusinessRepository(tmp_path / "business.db")
    repo.upsert_connection(
        {"id": "connection", "user": {"id": 100},
         "rights": {"can_reply": True}}, now,
    )
    session = repo.session("200", now, time(20), time(9, 30))
    repo.upsert_client("200", {"id": 200}, now)
    original = {
        "business_connection_id": "connection", "message_id": 1,
        "date": int(now.timestamp()),
        "chat": {"id": 200, "type": "private"},
        "from": {"id": 200}, "text": "+998 90 123 45 67",
    }
    assert repo.save_message(
        "connection", original, session["session_id"], "client", now,
    )

    assert repo.backfill_client_phone_index(now, limit=1) == 1
    assert repo.backfill_client_phone_index(now, limit=1) == 0
    with connect(repo.path) as db:
        assert db.execute(
            "SELECT phone_indexed_at FROM business_messages WHERE message_id=1"
        ).fetchone()[0]

    edited_at = now + timedelta(minutes=1)
    edited = {
        **original,
        "edit_date": int(edited_at.timestamp()),
        "text": "+998 93 765 43 21",
    }
    assert repo.edit_message(
        "connection", edited, edited_at, update_id=2,
    )
    with connect(repo.path) as db:
        assert db.execute(
            "SELECT phone_indexed_at FROM business_messages WHERE message_id=1"
        ).fetchone()[0] is None

    assert repo.backfill_client_phone_index(edited_at, limit=1) == 1
    with connect(repo.path) as db:
        active = db.execute(
            """SELECT phone_normalized FROM business_chat_phones
               WHERE deleted_at IS NULL"""
        ).fetchall()
        assert [row[0] for row in active] == ["+998937654321"]


def test_service_indexes_daytime_client_edits_and_revokes_delete_before_reply_gates(tmp_path):
    current = [datetime(2026, 9, 7, 12, 0, tzinfo=TZ)]
    service = BusinessService(
        settings(tmp_path / "business.db"),
        clock=lambda: current[0],
        api=object(),
        products=object(),
    )
    # No can_reply right: the message cannot reach any automation branch, but
    # its identity evidence must still be indexed around the clock.
    service.repo.upsert_connection(
        {"id": "connection", "user": {"id": 100},
         "is_enabled": True, "rights": {"can_reply": False}},
        current[0],
    )
    message = {
        "business_connection_id": "connection", "message_id": 10,
        "date": int(current[0].timestamp()),
        "chat": {"id": 200, "type": "private"},
        "from": {"id": 200, "language_code": "ru"},
        "text": "+998 90 123 45 67, +998 93 765 43 21",
    }
    original = {"update_id": 1, "business_message": message}
    assert service.repo.save_update(original, current[0])
    service.process_update(original)

    def active_phones() -> list[str]:
        with connect(service.repo.path) as db:
            return [
                row[0] for row in db.execute(
                    """SELECT phone_normalized FROM business_chat_phones
                       WHERE deleted_at IS NULL ORDER BY phone_normalized"""
                ).fetchall()
            ]

    assert active_phones() == ["+998901234567", "+998937654321"]

    current[0] += timedelta(minutes=1)
    edited = {
        "update_id": 2,
        "edited_business_message": {
            **message,
            "edit_date": int(current[0].timestamp()),
            "text": "+998 95 111 22 33",
        },
    }
    assert service.repo.save_update(edited, current[0])
    service.process_update(edited)
    assert active_phones() == ["+998951112233"]

    current[0] += timedelta(minutes=1)
    deleted = {
        "update_id": 3,
        "deleted_business_messages": {
            "business_connection_id": "connection",
            "chat": {"id": 200},
            "message_ids": [10],
        },
    }
    assert service.repo.save_update(deleted, current[0])
    service.process_update(deleted)
    assert active_phones() == []
