import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram_business.migrations import connect
from telegram_business.repository import (
    BusinessRepository,
    PendingBusinessCallbackError,
)
from telegram_business.scheduler import DurableScheduler, retry_after_seconds
from telegram_business.security import redact_payment_data, sanitize_telegram_payload
from telegram_business.service import BusinessService, RuntimePolicy
from telegram_business.telegram_api import TelegramAPIError


TZ = ZoneInfo("Asia/Tashkent")


class RetryableError(RuntimeError):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


class TelegramBusinessStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "business.db"
        self.repo = BusinessRepository(self.path)
        self.now = datetime(2026, 8, 22, 20, 0, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self, now):
        class API:
            def __init__(api_self):
                api_self.sent = []

            def send_message(api_self, connection_id, chat_id, text, **options):
                api_self.sent.append((connection_id, chat_id, text, options))
                return {"ok": True, "result": {"message_id": 900 + len(api_self.sent)}}

        class Sheets:
            def intents(sheets_self, _now):
                return ()

            def render(sheets_self, *args, **kwargs):
                raise RuntimeError("use builtin templates")

        service = BusinessService.__new__(BusinessService)
        service.settings = SimpleNamespace(
            allowed_connection_id="c", timezone="Asia/Tashkent"
        )
        service.repo = self.repo
        service.api = API()
        service.sheets = Sheets()
        service.products = SimpleNamespace(recognizes_query=lambda _query: False)
        service.bot_id = "999"
        service.clock = lambda: now
        service._recent_local = {}
        policy = RuntimePolicy(
            night_start=time(20), night_end=time(9, 30),
            manager_start=time(10), manager_end=time(20),
            workdays=frozenset(range(7)), final_idle_seconds=300,
            debounce_seconds=3, manager_lock_minutes=120,
            credit_cooldown_minutes=720, max_messages_10m=4,
            max_messages_session=8,
        )
        service._runtime_policy = lambda _now: policy
        return service, policy

    def test_additive_migration_preserves_legacy_rows(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        db = sqlite3.connect(legacy)
        db.execute("""CREATE TABLE business_updates(
            update_id INTEGER PRIMARY KEY,event_type TEXT NOT NULL,business_connection_id TEXT,
            chat_id TEXT,message_id INTEGER,raw_payload TEXT NOT NULL,received_at TEXT NOT NULL,
            processed_at TEXT,status TEXT NOT NULL DEFAULT 'new',error TEXT)""")
        db.execute("INSERT INTO business_updates(update_id,event_type,raw_payload,received_at) VALUES(1,'unknown','{}',?)",
                   (self.now.isoformat(),))
        db.execute("""CREATE TABLE scheduled_actions(
            action_id INTEGER PRIMARY KEY AUTOINCREMENT,dedupe_key TEXT NOT NULL UNIQUE,chat_id TEXT NOT NULL,
            session_id TEXT,action_type TEXT NOT NULL,execute_at TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
            executed_at TEXT,last_error TEXT)""")
        db.commit(); db.close()
        BusinessRepository(legacy)
        with connect(legacy) as migrated:
            self.assertEqual(migrated.execute("SELECT raw_payload FROM business_updates WHERE update_id=1").fetchone()[0], "{}")
            update_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(business_updates)")}
            action_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(scheduled_actions)")}
        self.assertIn("lease_token", update_columns)
        self.assertIn("callback_query_id", update_columns)
        self.assertIn("generation", action_columns)
        with connect(legacy) as migrated:
            tables = {
                row["name"]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            request_columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(business_requests)")
            }
            client_columns = {
                row["name"]
                for row in migrated.execute("PRAGMA table_info(business_clients)")
            }
            receipt_columns = {
                row["name"]
                for row in migrated.execute(
                    "PRAGMA table_info(business_callback_receipts)"
                )
            }
        self.assertTrue(
            {"business_requests", "business_request_events",
             "business_callback_tokens", "business_callback_receipts"}
            <= tables
        )
        self.assertIn("origin_update_id", request_columns)
        self.assertTrue(
            {
                "phone",
                "phone_verified",
                "phone_owner_user_id",
                "location_phone_pending",
            }.issubset(client_columns)
        )
        self.assertIn("result", receipt_columns)

    def test_update_claim_is_atomic_and_stale_lease_recovers(self):
        update = {"update_id": 10, "business_connection": {"id": "c"}}
        self.assertTrue(self.repo.save_update(update, self.now))
        claimed = self.repo.claim_due_updates(self.now, lease_seconds=5)
        self.assertEqual([row["update_id"] for row in claimed], [10])
        self.assertEqual(self.repo.claim_due_updates(self.now), [])

        recovered = self.repo.recover_stale(self.now + timedelta(seconds=6))
        self.assertEqual(recovered["updates"], 1)
        claimed_again = self.repo.claim_due_updates(self.now + timedelta(seconds=6))
        self.assertEqual(len(claimed_again), 1)
        self.assertEqual(claimed_again[0]["attempts"], 2)

    def test_expired_update_worker_cannot_finish_newer_claim(self):
        self.repo.save_update({"update_id": 11, "business_connection": {"id": "c"}}, self.now)
        old = self.repo.claim_due_updates(self.now, lease_seconds=5)[0]
        later = self.now + timedelta(seconds=6)
        new = self.repo.claim_due_updates(later, lease_seconds=30)[0]
        with self.repo.bind_update_claim(11, old["lease_token"]):
            self.assertFalse(self.repo.mark_update(11, "processed", later))
        current = self.repo.update(11)
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["lease_token"], new["lease_token"])
        with self.repo.bind_update_claim(11, new["lease_token"]):
            self.assertTrue(self.repo.mark_update(11, "processed", later))

    def test_claimed_update_error_keeps_lease_until_backoff_is_set(self):
        self.repo.save_update({"update_id": 12, "business_connection": {"id": "c"}}, self.now)
        claimed = self.repo.claim_due_updates(self.now, lease_seconds=30)[0]
        with self.repo.bind_update_claim(12, claimed["lease_token"]):
            self.assertTrue(self.repo.mark_update(12, "error", self.now, "temporary"))
        current = self.repo.update(12)
        self.assertEqual(current["status"], "error")
        self.assertEqual(current["lease_token"], claimed["lease_token"])
        self.assertEqual(self.repo.claim_due_updates(self.now), [])
        self.assertTrue(self.repo.retry_update(
            12, self.now, "temporary", lease_token=claimed["lease_token"]
        ))
        self.assertEqual(self.repo.update(12)["status"], "retry")

    def test_same_update_can_continue_after_message_insert_only(self):
        session = self.repo.session("42", self.now)
        message = {"message_id": 5, "date": int(self.now.timestamp()), "chat": {"id": 42}, "text": "hello"}
        self.assertTrue(self.repo.save_message(
            "c", message, session["session_id"], "client", self.now, update_id=50
        ))
        self.assertTrue(self.repo.save_message(
            "c", message, session["session_id"], "client", self.now, update_id=50
        ))
        self.assertFalse(self.repo.save_message(
            "c", message, session["session_id"], "client", self.now, update_id=51
        ))

    def test_update_retry_backoff_and_multiple_edit_revisions(self):
        for update_id, text in ((20, "one"), (21, "two")):
            update = {"update_id": update_id, "edited_business_message": {
                "business_connection_id": "c", "message_id": 7,
                "chat": {"id": 2}, "text": text,
            }}
            self.assertTrue(self.repo.save_update(update, self.now))
        claimed = self.repo.claim_due_updates(self.now)
        self.assertEqual({row["update_id"] for row in claimed}, {20, 21})
        row = next(item for item in claimed if item["update_id"] == 20)
        self.assertTrue(self.repo.retry_update(20, self.now, "temporary", lease_token=row["lease_token"], retry_after=17))
        saved = self.repo.update(20)
        self.assertEqual(saved["status"], "retry")
        self.assertGreaterEqual(datetime.fromisoformat(saved["next_attempt_at"]), self.now + timedelta(seconds=17))

    def test_action_generation_fences_old_worker_and_updates_session(self):
        self.repo.schedule("debounce:42", "42", "old", "debounce", self.now, {"n": 1}, self.now)
        old = self.repo.claim_due_actions(self.now)[0]
        self.repo.schedule("debounce:42", "42", "new", "debounce", self.now + timedelta(seconds=3), {"n": 2}, self.now)
        self.assertFalse(self.repo.finish_action(old["action_id"], self.now, lease_token=old["lease_token"], generation=old["generation"]))
        due = self.repo.claim_due_actions(self.now + timedelta(seconds=3))[0]
        self.assertEqual(due["session_id"], "new")
        self.assertEqual(json.loads(due["payload"]), {"n": 2})
        self.assertGreater(due["generation"], old["generation"])

    def test_restart_backlog_keeps_distant_debounce_bursts_in_event_order(self):
        session = self.repo.session("42", self.now)
        complaint_at = self.now + timedelta(hours=1)
        model_at = complaint_at + timedelta(minutes=10)
        due_at = self.now + timedelta(seconds=3)

        # Simulate an out-of-order retry drain: the newer Telegram event is
        # persisted first, but the older complaint must still execute first.
        self.repo.schedule_debounce(
            "42", session["session_id"], model_at, due_at,
            {"connection_id": "c", "message_id": 2, "text": "iphone"},
            self.now, 3,
        )
        self.repo.schedule_debounce(
            "42", session["session_id"], complaint_at, due_at,
            {"connection_id": "c", "message_id": 1, "text": "complaint"},
            self.now, 3,
        )

        restarted = BusinessRepository(self.path)
        first = restarted.claim_due_actions(due_at, limit=1)[0]
        self.assertEqual(json.loads(first["payload"])["message_id"], 1)
        restarted.finish_action(
            first["action_id"], due_at, lease_token=first["lease_token"],
            generation=first["generation"],
        )
        second = restarted.claim_due_actions(due_at, limit=1)[0]
        self.assertEqual(json.loads(second["payload"])["message_id"], 2)

    def test_concurrent_debounce_reschedule_merges_one_telegram_burst(self):
        session = self.repo.session("42", self.now)
        barrier = threading.Barrier(2)
        errors = []

        def schedule(message_id, event_at):
            try:
                barrier.wait()
                BusinessRepository(self.path).schedule_debounce(
                    "42", session["session_id"], event_at,
                    self.now + timedelta(seconds=3),
                    {"connection_id": "c", "message_id": message_id},
                    self.now, 3,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=schedule, args=(1, self.now)),
            threading.Thread(
                target=schedule, args=(2, self.now + timedelta(seconds=2))
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        with connect(self.path) as db:
            actions = db.execute(
                """SELECT * FROM scheduled_actions WHERE action_type='debounce'
                   AND status='pending'"""
            ).fetchall()
        self.assertEqual(len(actions), 1)
        payload = json.loads(actions[0]["payload"])
        self.assertEqual(datetime.fromisoformat(payload["burst_start_at"]), self.now)
        self.assertEqual(
            datetime.fromisoformat(payload["event_at"]),
            self.now + timedelta(seconds=2),
        )
        self.assertEqual(payload["message_id"], 2)

    def test_delayed_older_burst_fences_running_newer_burst(self):
        session = self.repo.session("42", self.now)
        complaint_at = self.now + timedelta(hours=1)
        model_at = complaint_at + timedelta(minutes=10)
        self.repo.schedule_debounce(
            "42", session["session_id"], model_at, self.now,
            {"connection_id": "c", "message_id": 2}, self.now, 3,
        )
        newer = self.repo.claim_due_actions(self.now, limit=1)[0]
        self.repo.schedule_debounce(
            "42", session["session_id"], complaint_at, self.now,
            {"connection_id": "c", "message_id": 1}, self.now, 3,
        )

        self.assertFalse(self.repo.action_is_current(
            newer["action_id"], newer["lease_token"], newer["generation"]
        ))
        older = self.repo.claim_due_actions(self.now, limit=1)[0]
        self.assertEqual(json.loads(older["payload"])["message_id"], 1)
        self.repo.finish_action(
            older["action_id"], self.now, lease_token=older["lease_token"],
            generation=older["generation"],
        )
        retried_newer = self.repo.claim_due_actions(self.now, limit=1)[0]
        self.assertEqual(json.loads(retried_newer["payload"])["message_id"], 2)

    def test_burst_anchor_never_consumes_future_backlog_message(self):
        service, policy = self._service(self.now)
        session = self.repo.session("42", self.now)
        complaint_at = self.now + timedelta(hours=1)
        model_at = complaint_at + timedelta(minutes=10)
        for update_id, message_id, event_at, text_value in (
            (1, 1, complaint_at, "жалоба"),
            (2, 2, model_at, "iphone 16 pro max"),
        ):
            self.repo.save_message(
                "c",
                {"message_id": message_id, "date": int(event_at.timestamp()),
                 "chat": {"id": 42}, "from": {"id": 42}, "text": text_value},
                session["session_id"], "client", self.now, update_id=update_id,
            )
        rows = self.repo.session_messages(session["session_id"])
        complaint = service._burst(rows, complaint_at, policy, 1)
        model = service._burst(rows, model_at, policy, 2)
        self.assertEqual([row["text"] for row in complaint], ["жалоба"])
        self.assertEqual([row["text"] for row in model], ["iphone 16 pro max"])

    def test_backlog_complaint_handoff_runs_before_and_cancels_later_model(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42, "language_code": "ru"}, self.now)
        session = self.repo.session("42", self.now)
        complaint_at = self.now + timedelta(hours=1)
        model_at = complaint_at + timedelta(minutes=10)
        for update_id, message_id, event_at, text_value in (
            (1, 1, complaint_at, "жалоба на товар"),
            (2, 2, model_at, "iphone 16 pro max"),
        ):
            self.repo.save_message(
                "c",
                {"message_id": message_id, "date": int(event_at.timestamp()),
                 "chat": {"id": 42}, "from": {"id": 42}, "text": text_value},
                session["session_id"], "client", self.now, update_id=update_id,
            )
            self.repo.touch_client_message(
                "42", session["session_id"], self.now,
                event_at=event_at, message_id=message_id,
            )
        self.repo.schedule_debounce(
            "42", session["session_id"], model_at, self.now,
            {"connection_id": "c", "message_id": 2}, self.now, 3,
        )
        self.repo.schedule_debounce(
            "42", session["session_id"], complaint_at, self.now,
            {"connection_id": "c", "message_id": 1}, self.now, 3,
        )

        complaint_action = self.repo.claim_due_actions(self.now, limit=1)[0]
        with self.repo.bind_action_claim(
            complaint_action["action_id"], complaint_action["lease_token"],
            complaint_action["generation"],
        ):
            service.execute(complaint_action)

        saved_session = self.repo.session_by_id(session["session_id"])
        with connect(self.path) as db:
            model_action = db.execute(
                """SELECT * FROM scheduled_actions WHERE action_type='debounce'
                   AND json_extract(payload,'$.message_id')=2"""
            ).fetchone()
        self.assertEqual(saved_session["handoff_reason"], "complaint")
        self.assertEqual(saved_session["status"], "human_handoff")
        self.assertEqual(model_action["status"], "cancelled")
        self.assertEqual(len(service.api.sent), 1)

    def test_same_client_update_after_manager_does_not_reopen_or_reschedule(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        client = {
            "update_id": 1,
            "business_message": {
                "business_connection_id": "c", "message_id": 1,
                "date": int(self.now.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "language_code": "ru"}, "text": "iphone",
            },
        }
        manager_at = self.now + timedelta(seconds=1)
        manager = {
            "update_id": 2,
            "business_message": {
                "business_connection_id": "c", "message_id": 2,
                "date": int(manager_at.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "ответ менеджера",
            },
        }
        service.process_update(client)
        service.clock = lambda: manager_at
        service.process_update(manager)
        service.clock = lambda: manager_at + timedelta(seconds=1)
        service.process_update(client)

        with connect(self.path) as db:
            cycles = db.execute("SELECT * FROM response_cycles").fetchall()
            live_actions = db.execute(
                """SELECT * FROM scheduled_actions
                   WHERE status IN ('pending','running')"""
            ).fetchall()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["status"], "manager_answered")
        self.assertEqual(live_actions, [])

    def test_manager_before_delayed_client_persists_lock_and_watermark(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        client_at = self.now
        manager_at = self.now + timedelta(seconds=1)
        manager = {
            "update_id": 2,
            "business_message": {
                "business_connection_id": "c", "message_id": 2,
                "date": int(manager_at.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "ответ",
            },
        }
        client = {
            "update_id": 1,
            "business_message": {
                "business_connection_id": "c", "message_id": 1,
                "date": int(client_at.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42}, "text": "жалоба",
            },
        }
        service.clock = lambda: manager_at
        service.process_update(manager)
        service.clock = lambda: manager_at + timedelta(seconds=1)
        service.process_update(client)

        saved_client = self.repo.client("42")
        with connect(self.path) as db:
            waiting = db.execute(
                "SELECT count(*) FROM response_cycles WHERE status='waiting_manager'"
            ).fetchone()[0]
            live_actions = db.execute(
                """SELECT count(*) FROM scheduled_actions
                   WHERE status IN ('pending','running')"""
            ).fetchone()[0]
        self.assertIsNotNone(saved_client["manager_lock_until"])
        self.assertEqual(waiting, 0)
        self.assertEqual(live_actions, 0)

    def test_saved_manual_webhook_fences_actions_before_worker_processing(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        session = self.repo.session("42", self.now)
        self.repo.schedule(
            "final:fenced", "42", session["session_id"], "final",
            self.now, {"connection_id": "c"}, self.now,
        )
        manager = {
            "update_id": 90,
            "business_message": {
                "business_connection_id": "c", "message_id": 50,
                "date": int(self.now.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "ручной ответ",
            },
        }
        self.assertTrue(self.repo.save_update(manager, self.now))
        with connect(self.path) as db:
            action = db.execute(
                "SELECT status FROM scheduled_actions WHERE dedupe_key='final:fenced'"
            ).fetchone()
            fences = db.execute(
                "SELECT count(*) FROM business_manager_fences WHERE chat_id='42'"
            ).fetchone()[0]
        self.assertEqual(action["status"], "cancelled")
        self.assertEqual(fences, 1)
        self.assertTrue(self.repo.manager_fence_active("42", self.now, 120))

    def test_foreign_connection_manager_webhook_cannot_fast_fence_chat(self):
        self.repo.upsert_connection(
            {"id": "foreign", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        session = self.repo.session("42", self.now)
        self.repo.schedule(
            "final:foreign", "42", session["session_id"], "final",
            self.now, {"connection_id": "c"}, self.now,
        )
        manager = {
            "update_id": 91,
            "business_message": {
                "business_connection_id": "foreign", "message_id": 51,
                "date": int(self.now.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "чужой ручной ответ",
            },
        }
        self.assertTrue(
            self.repo.save_update(
                manager, self.now, allowed_connection_id="c"
            )
        )
        with connect(self.path) as db:
            action = db.execute(
                "SELECT status FROM scheduled_actions WHERE dedupe_key='final:foreign'"
            ).fetchone()
            fences = db.execute(
                "SELECT count(*) FROM business_manager_fences WHERE chat_id='42'"
            ).fetchone()[0]
        self.assertEqual(action["status"], "pending")
        self.assertEqual(fences, 0)

    def test_allowed_connection_revoke_is_persisted_and_cancels_actions_at_ingest(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        session = self.repo.session("42", self.now)
        self.repo.schedule(
            "final:revoked", "42", session["session_id"], "final",
            self.now, {"connection_id": "c"}, self.now,
        )
        revoked = {
            "update_id": 92,
            "business_connection": {
                "id": "c", "user": {"id": 100}, "is_enabled": False,
                "rights": {"can_reply": True},
            },
        }
        self.assertTrue(
            self.repo.save_update(
                revoked, self.now, allowed_connection_id="c"
            )
        )
        with connect(self.path) as db:
            action = db.execute(
                "SELECT status FROM scheduled_actions WHERE dedupe_key='final:revoked'"
            ).fetchone()
        self.assertFalse(self.repo.connection_can_reply("c"))
        self.assertEqual(action["status"], "cancelled")

    def test_stale_manager_event_does_not_touch_newer_client_cycle_or_action(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        client_at = self.now + timedelta(minutes=10)
        processed_at = client_at + timedelta(minutes=1)
        self.repo.upsert_client("42", {"id": 42}, processed_at)
        session = self.repo.session("42", client_at)
        client = {
            "business_connection_id": "c", "message_id": 20,
            "date": int(client_at.timestamp()),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42}, "text": "iPhone 16 Pro Max",
        }
        self.assertTrue(self.repo.save_message(
            "c", client, session["session_id"], "client", processed_at,
            update_id=20,
        ))
        cycle_id = self.repo.touch_client_message(
            "42", session["session_id"], processed_at,
            event_at=client_at, message_id=20,
        )
        self.repo.schedule(
            "debounce:newer", "42", session["session_id"], "debounce",
            processed_at, {"connection_id": "c", "message_id": 20},
            processed_at,
        )

        # This webhook was generated before the client message but delivered
        # after it. Persisting the webhook must not fast-cancel newer work.
        manager = {
            "update_id": 10,
            "business_message": {
                "business_connection_id": "c", "message_id": 10,
                "date": int(self.now.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "старый ответ",
            },
        }
        self.assertTrue(self.repo.save_update(manager, processed_at))
        self.assertFalse(self.repo.manager_fence_active("42", processed_at, 120))
        self.assertTrue(self.repo.save_message(
            "c", manager["business_message"], session["session_id"],
            "manager", processed_at, update_id=10,
        ))
        self.repo.manager_answer(
            "42", processed_at, 120, event_at=self.now, message_id=10,
            session_id=session["session_id"],
        )

        with connect(self.path) as db:
            cycle = db.execute(
                "SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            action = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key='debounce:newer'"
            ).fetchone()
            saved_session = db.execute(
                "SELECT * FROM business_sessions WHERE session_id=?",
                (session["session_id"],),
            ).fetchone()
        saved_client = self.repo.client("42")
        self.assertEqual(cycle["status"], "waiting_manager")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(saved_session["status"], "waiting_manager")
        self.assertEqual(
            datetime.fromisoformat(saved_client["manager_lock_until"]),
            self.now + timedelta(minutes=120),
        )
        self.assertTrue(
            self.repo.manager_lock_covers_event("42", client_at)
        )
        self.assertFalse(
            self.repo.manager_lock_covers_event(
                "42", self.now + timedelta(minutes=121),
            )
        )

    def test_manager_lock_uses_event_time_and_older_answer_cannot_shorten_it(self):
        session = self.repo.session("42", self.now)
        newer_answer_at = self.now + timedelta(minutes=20)
        processed_at = newer_answer_at + timedelta(hours=1)
        self.repo.manager_answer(
            "42", processed_at, 120, event_at=newer_answer_at,
            message_id=30, session_id=session["session_id"],
        )
        expected = newer_answer_at + timedelta(minutes=120)
        self.assertEqual(
            datetime.fromisoformat(self.repo.client("42")["manager_lock_until"]),
            expected,
        )

        # A delayed older manager event must not move the existing lock back.
        self.repo.manager_answer(
            "42", processed_at + timedelta(minutes=1), 120,
            event_at=self.now + timedelta(minutes=5), message_id=25,
            session_id=session["session_id"],
        )
        self.assertEqual(
            datetime.fromisoformat(self.repo.client("42")["manager_lock_until"]),
            expected,
        )

    def test_manager_between_two_clients_splits_cycle_by_telegram_chronology(self):
        first_at = datetime(2026, 8, 23, 10, 0, tzinfo=TZ)
        manager_at = first_at + timedelta(minutes=1)
        second_at = first_at + timedelta(minutes=2)
        processed_at = second_at + timedelta(minutes=1)
        self.repo.upsert_client("42", {"id": 42}, processed_at)
        session = self.repo.session("42", first_at)

        for update_id, message_id, event_at, text in (
            (100, 10, first_at, "первый вопрос"),
            (102, 12, second_at, "второй вопрос"),
        ):
            message = {
                "message_id": message_id, "date": int(event_at.timestamp()),
                "chat": {"id": 42}, "from": {"id": 42}, "text": text,
            }
            self.assertTrue(self.repo.save_message(
                "c", message, session["session_id"], "client", processed_at,
                update_id=update_id,
            ))
            original_cycle_id = self.repo.touch_client_message(
                "42", session["session_id"], processed_at,
                event_at=event_at, message_id=message_id,
            )

        self.repo.schedule(
            "debounce:chronology", "42", session["session_id"], "debounce",
            processed_at, {"connection_id": "c", "message_id": 12},
            processed_at,
        )
        manager = {
            "message_id": 11, "date": int(manager_at.timestamp()),
            "chat": {"id": 42}, "from": {"id": 100}, "text": "ответ",
        }
        self.repo.save_message(
            "c", manager, session["session_id"], "manager", processed_at,
            update_id=101,
        )
        self.repo.manager_answer(
            "42", processed_at, 120, event_at=manager_at, message_id=11,
            session_id=session["session_id"],
        )

        with connect(self.path) as db:
            cycles = db.execute(
                "SELECT * FROM response_cycles ORDER BY first_client_at"
            ).fetchall()
            messages = {
                row["message_id"]: row for row in db.execute(
                    "SELECT * FROM business_messages ORDER BY message_id"
                ).fetchall()
            }
            action = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key='debounce:chronology'"
            ).fetchone()
            saved_session = db.execute(
                "SELECT * FROM business_sessions WHERE session_id=?",
                (session["session_id"],),
            ).fetchone()
        self.assertEqual(len(cycles), 2)
        answered, waiting = cycles
        self.assertEqual(answered["cycle_id"], original_cycle_id)
        self.assertEqual(answered["status"], "manager_answered")
        self.assertEqual(datetime.fromisoformat(answered["first_client_at"]), first_at)
        self.assertEqual(datetime.fromisoformat(answered["last_client_at"]), first_at)
        self.assertEqual(datetime.fromisoformat(answered["first_manager_at"]), manager_at)
        self.assertEqual(answered["calendar_response_seconds"], 60)
        self.assertEqual(answered["work_response_seconds"], 60)
        self.assertEqual(waiting["status"], "waiting_manager")
        self.assertEqual(datetime.fromisoformat(waiting["first_client_at"]), second_at)
        self.assertEqual(datetime.fromisoformat(waiting["last_client_at"]), second_at)
        self.assertEqual(messages[10]["cycle_id"], answered["cycle_id"])
        self.assertEqual(messages[11]["cycle_id"], answered["cycle_id"])
        self.assertEqual(messages[12]["cycle_id"], waiting["cycle_id"])
        self.assertEqual(action["status"], "pending")
        self.assertEqual(saved_session["status"], "waiting_manager")

    def test_handoff_resolves_on_manager_answer_and_resumes_after_lock_expiry(self):
        client_at = self.now
        manager_at = client_at + timedelta(minutes=1)
        self.repo.upsert_client("42", {"id": 42}, client_at)
        session = self.repo.session("42", client_at)
        first = {
            "message_id": 1, "date": int(client_at.timestamp()),
            "chat": {"id": 42}, "from": {"id": 42}, "text": "жалоба",
        }
        self.repo.save_message(
            "c", first, session["session_id"], "client", client_at,
            update_id=1,
        )
        self.repo.touch_client_message(
            "42", session["session_id"], client_at,
            event_at=client_at, message_id=1,
        )
        self.repo.stop_session_automation(
            session["session_id"], client_at, "complaint",
        )
        self.assertFalse(self.repo.session_may_automate(session["session_id"]))

        manager = {
            "message_id": 2, "date": int(manager_at.timestamp()),
            "chat": {"id": 42}, "from": {"id": 100}, "text": "решено",
        }
        self.repo.save_message(
            "c", manager, session["session_id"], "manager", manager_at,
            update_id=2,
        )
        self.repo.manager_answer(
            "42", manager_at, 120, event_at=manager_at, message_id=2,
            session_id=session["session_id"],
        )
        resolved = self.repo.session_by_id(session["session_id"])
        self.assertEqual(resolved["status"], "manager_answered")
        self.assertEqual(resolved["automation_handoff"], 0)
        self.assertEqual(resolved["search_disabled"], 0)
        self.assertEqual(resolved["handoff_reason"], "complaint")

        after_lock = manager_at + timedelta(minutes=120, seconds=1)
        followup = {
            "message_id": 3, "date": int(after_lock.timestamp()),
            "chat": {"id": 42}, "from": {"id": 42},
            "text": "iPhone 16 Pro Max",
        }
        self.repo.save_message(
            "c", followup, session["session_id"], "client", after_lock,
            update_id=3,
        )
        new_cycle = self.repo.touch_client_message(
            "42", session["session_id"], after_lock,
            event_at=after_lock, message_id=3,
        )
        self.assertTrue(new_cycle)
        self.assertTrue(self.repo.session_may_automate(session["session_id"]))
        self.assertTrue(self.repo.may_automate("42", after_lock))
        resumed = self.repo.session_by_id(session["session_id"])
        self.assertEqual(resumed["status"], "waiting_manager")
        self.assertEqual(resumed["handoff_reason"], "complaint")

        # A permanent exclusion remains authoritative across manual answers.
        self.assertTrue(self.repo.set_bot_paused(
            "42", True, after_lock, "active_order",
        ))
        self.repo.manager_answer(
            "42", after_lock + timedelta(minutes=1), 120,
            event_at=after_lock + timedelta(minutes=1), message_id=4,
            session_id=session["session_id"],
        )
        self.assertTrue(self.repo.is_bot_paused("42"))
        self.assertFalse(self.repo.may_automate(
            "42", after_lock + timedelta(minutes=121),
        ))

    def test_bot_and_offline_webhooks_never_create_fast_manager_fence(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        for update_id, extra in (
            (91, {"sender_business_bot": {"id": 999}}),
            (92, {"is_from_offline": True}),
        ):
            event = {
                "business_connection_id": "c", "message_id": update_id,
                "date": int(self.now.timestamp()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 100}, "text": "auto", **extra,
            }
            self.assertTrue(self.repo.save_update(
                {"update_id": update_id, "business_message": event}, self.now,
            ))
        with connect(self.path) as db:
            fences = db.execute(
                "SELECT count(*) FROM business_manager_fences"
            ).fetchone()[0]
        self.assertEqual(fences, 0)

    def test_edit_before_original_keeps_revision_and_processes_original_once(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        original_at = self.now + timedelta(minutes=5)
        edit = {
            "message_id": 70,
            "date": int(original_at.timestamp()),
            "edit_date": int((original_at + timedelta(seconds=2)).timestamp()),
            "chat": {"id": 42}, "from": {"id": 42},
            "text": "iPhone 16 Pro Max 256 GB",
        }
        self.assertTrue(self.repo.edit_message(
            "c", edit, original_at + timedelta(seconds=2), update_id=101,
            night_start=time(18), night_end=time(8),
        ))
        session = self.repo.session("42", original_at, time(18), time(8))
        original = {
            "message_id": 70, "date": int(original_at.timestamp()),
            "chat": {"id": 42}, "from": {"id": 42}, "text": "iphone",
        }
        self.assertTrue(self.repo.save_message(
            "c", original, session["session_id"], "client", self.now,
            update_id=100,
        ))
        # Retrying that same original update may finish touch/scheduling, while
        # another Telegram update with the same message remains a duplicate.
        self.assertTrue(self.repo.save_message(
            "c", original, session["session_id"], "client", self.now,
            update_id=100,
        ))
        self.assertFalse(self.repo.save_message(
            "c", original, session["session_id"], "client", self.now,
            update_id=102,
        ))
        with connect(self.path) as db:
            row = db.execute(
                "SELECT * FROM business_messages WHERE message_id=70"
            ).fetchone()
        self.assertEqual(row["text"], "iPhone 16 Pro Max 256 GB")
        self.assertEqual(row["session_id"], session["session_id"])
        self.assertEqual(row["original_received"], 1)

    def test_ambiguous_final_transport_is_never_sent_twice(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )

        class AmbiguousAPI:
            def __init__(api_self):
                api_self.calls = 0

            def send_message(api_self, *args, **kwargs):
                api_self.calls += 1
                raise RuntimeError("connection vanished after write")

        service.api = AmbiguousAPI()
        with self.assertRaises(RuntimeError):
            service.send(
                "c", "42", session["session_id"], "final", "final", self.now,
            )
        # A recovered action treats the unknown first outcome as delivered.  It
        # repairs session flags through the True return without a second API call.
        self.assertTrue(service.send(
            "c", "42", session["session_id"], "final", "final", self.now,
        ))
        self.assertEqual(service.api.calls, 1)
        with connect(self.path) as db:
            delivery = db.execute(
                "SELECT * FROM business_outbound_deliveries"
            ).fetchone()
        self.assertEqual(delivery["state"], "uncertain")

    def test_ambiguous_typed_error_wins_over_retryable_flag(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )

        class AmbiguousAPI:
            def __init__(api_self):
                api_self.calls = 0

            def send_message(api_self, *args, **kwargs):
                api_self.calls += 1
                raise TelegramAPIError(
                    "ambiguous server response", status=500, retryable=True,
                    ambiguous=True,
                )

        service.api = AmbiguousAPI()
        with self.assertRaises(TelegramAPIError):
            service.send(
                "c", "42", session["session_id"], "final", "final", self.now,
            )
        self.assertTrue(service.send(
            "c", "42", session["session_id"], "final", "final", self.now,
        ))
        self.assertEqual(service.api.calls, 1)
        with connect(self.path) as db:
            delivery = db.execute(
                "SELECT * FROM business_outbound_deliveries"
            ).fetchone()
        self.assertEqual(delivery["state"], "uncertain")

    def test_allowed_connection_is_rechecked_immediately_before_send(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "foreign", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )
        self.assertFalse(service.send(
            "foreign", "42", session["session_id"], "reply", "final", self.now,
        ))
        self.assertEqual(service.api.sent, [])

    def test_stable_event_delivery_is_recorded_once(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )
        for _ in range(2):
            self.assertTrue(service.send(
                "c", "42", session["session_id"], "order reply",
                "order_request", self.now,
                delivery_key="client-message:1",
            ))
        self.assertEqual(len(service.api.sent), 1)

    def test_greeting_is_once_per_session_but_handoff_is_per_client_event(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        session_id = session["session_id"]
        self.repo.touch_client_message(
            "42", session_id, self.now, event_at=self.now, message_id=1,
        )
        self.assertTrue(service.send(
            "c", "42", session_id, "short greeting", "greeting_model",
            self.now, delivery_key=f"greeting:{session_id}",
        ))
        self.assertTrue(service.send(
            "c", "42", session_id, "long greeting", "greeting_no_model",
            self.now, delivery_key=f"greeting:{session_id}",
        ))
        self.assertTrue(service.send(
            "c", "42", session_id, "handoff one", "human_handoff",
            self.now, delivery_key="client-message:2",
        ))
        self.assertTrue(service.send(
            "c", "42", session_id, "handoff one", "human_handoff",
            self.now, delivery_key="client-message:2",
        ))
        # After a manager cycle has ended, a new client message in the same
        # night session may legitimately require a fresh handoff.
        self.assertTrue(service.send(
            "c", "42", session_id, "handoff two", "human_handoff",
            self.now, delivery_key="client-message:3",
        ))
        self.assertEqual(len(service.api.sent), 3)

    def test_product_fingerprint_dedupes_within_but_not_across_sessions(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        first = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", first["session_id"], self.now,
            event_at=self.now, message_id=1,
        )
        for _ in range(2):
            self.assertTrue(service.send(
                "c", "42", first["session_id"], "prices", "product_result",
                self.now, discriminator="fingerprint",
                delivery_key="product:fingerprint",
            ))
        second_at = self.now + timedelta(days=1)
        second = self.repo.session("42", second_at)
        self.repo.touch_client_message(
            "42", second["session_id"], second_at,
            event_at=second_at, message_id=2,
        )
        self.assertTrue(service.send(
            "c", "42", second["session_id"], "prices", "product_result",
            second_at, discriminator="fingerprint",
            delivery_key="product:fingerprint",
        ))
        self.assertEqual(len(service.api.sent), 2)

    def test_definite_rate_limit_keeps_critical_delivery_retryable(self):
        service, _ = self._service(self.now)
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )

        class RateLimitedAPI:
            def __init__(api_self):
                api_self.calls = 0

            def send_message(api_self, *args, **kwargs):
                api_self.calls += 1
                if api_self.calls == 1:
                    raise TelegramAPIError(
                        "rate limited", status=429, retryable=True,
                        retry_after=7,
                    )
                return {"ok": True, "result": {"message_id": 777}}

        service.api = RateLimitedAPI()
        with self.assertRaises(TelegramAPIError):
            service.send(
                "c", "42", session["session_id"], "credit", "credit", self.now,
            )
        self.assertTrue(service.send(
            "c", "42", session["session_id"], "credit", "credit", self.now,
        ))
        self.assertEqual(service.api.calls, 2)
        with connect(self.path) as db:
            delivery = db.execute(
                "SELECT * FROM business_outbound_deliveries"
            ).fetchone()
        self.assertEqual(delivery["state"], "sent")

    def test_action_retry_after_and_stale_running_recovery(self):
        self.repo.schedule("final:s", "42", "s", "final", self.now, {}, self.now)
        action = self.repo.claim_due_actions(self.now, lease_seconds=5)[0]
        self.assertTrue(self.repo.finish_action(action["action_id"], self.now, "429", lease_token=action["lease_token"],
                                                generation=action["generation"], retry_after=30))
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM scheduled_actions").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertGreaterEqual(datetime.fromisoformat(row["execute_at"]), self.now + timedelta(seconds=30))

        claimed = self.repo.claim_due_actions(self.now + timedelta(seconds=30), lease_seconds=5)[0]
        self.assertEqual(self.repo.recover_stale(self.now + timedelta(seconds=36))["actions"], 1)
        with connect(self.path) as db:
            recovered = db.execute("SELECT * FROM scheduled_actions WHERE action_id=?", (claimed["action_id"],)).fetchone()
        self.assertEqual(recovered["status"], "pending")
        self.assertGreater(recovered["generation"], claimed["generation"])

    def test_manager_cancel_fences_claimed_action(self):
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message("42", session["session_id"], self.now)
        self.repo.schedule("final:s", "42", session["session_id"], "final", self.now, {}, self.now)
        action = self.repo.claim_due_actions(self.now)[0]
        self.repo.manager_answer("42", self.now, 120, session_id=session["session_id"])
        self.assertFalse(self.repo.action_is_current(action["action_id"], action["lease_token"], action["generation"]))
        self.assertFalse(self.repo.finish_action(action["action_id"], self.now, lease_token=action["lease_token"], generation=action["generation"]))

    def test_reschedule_invalidates_action_context_before_send(self):
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message("42", session["session_id"], self.now)
        self.repo.schedule("debounce:42", "42", session["session_id"], "debounce", self.now, {}, self.now)
        claimed = self.repo.claim_due_actions(self.now)[0]
        with self.repo.bind_action_claim(
            claimed["action_id"], claimed["lease_token"], claimed["generation"]
        ):
            self.assertTrue(self.repo.may_automate("42", self.now))
            self.repo.schedule(
                "debounce:42", "42", session["session_id"], "debounce",
                self.now + timedelta(seconds=3), {}, self.now,
            )
            self.assertFalse(self.repo.may_automate("42", self.now))

    def test_disabled_connection_fences_pending_and_running_replies(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 1}, "is_enabled": True,
             "rights": {"can_reply": True}}, self.now,
        )
        self.repo.schedule("reply:42", "42", "s", "final", self.now, {}, self.now)
        claimed = self.repo.claim_due_actions(self.now)[0]
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 1}, "is_enabled": False,
             "rights": {"can_reply": False}}, self.now,
        )
        self.assertFalse(self.repo.connection_can_reply("c"))
        self.assertFalse(self.repo.action_is_current(
            claimed["action_id"], claimed["lease_token"], claimed["generation"]
        ))

    def test_stale_final_with_recorded_bot_message_is_not_requeued(self):
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message("42", session["session_id"], self.now)
        self.repo.patch_session(session["session_id"], self.now, price_sent=1)
        self.repo.schedule("final:s", "42", session["session_id"], "final", self.now, {}, self.now)
        action = self.repo.claim_due_actions(self.now, lease_seconds=5)[0]
        self.repo.record_bot_message(
            "c", "42", session["session_id"], 100, "final text", "final", self.now
        )
        self.repo.recover_stale(self.now + timedelta(seconds=6))
        with connect(self.path) as db:
            saved_action = db.execute(
                "SELECT * FROM scheduled_actions WHERE action_id=?", (action["action_id"],)
            ).fetchone()
            saved_session = db.execute(
                "SELECT * FROM business_sessions WHERE session_id=?", (session["session_id"],)
            ).fetchone()
        self.assertEqual(saved_action["status"], "done")
        self.assertEqual(saved_session["final_sent"], 1)

    def test_reused_credit_action_ignores_previous_generation_message(self):
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        self.repo.touch_client_message("42", session["session_id"], self.now)
        self.repo.schedule("credit:42", "42", session["session_id"], "credit", self.now, {}, self.now)
        first = self.repo.claim_due_actions(self.now)[0]
        self.repo.record_bot_message(
            "c", "42", session["session_id"], 101, "credit", "credit",
            self.now + timedelta(seconds=1),
        )
        self.repo.finish_action(
            first["action_id"], self.now + timedelta(seconds=1),
            lease_token=first["lease_token"], generation=first["generation"],
        )
        later = self.now + timedelta(days=1)
        later_session = self.repo.session("42", later)
        self.repo.schedule(
            "credit:42", "42", later_session["session_id"], "credit", later, {}, later
        )
        second = self.repo.claim_due_actions(later, lease_seconds=5)[0]
        self.repo.recover_stale(later + timedelta(seconds=6))
        with connect(self.path) as db:
            saved = db.execute(
                "SELECT * FROM scheduled_actions WHERE action_id=?", (second["action_id"],)
            ).fetchone()
        self.assertEqual(saved["status"], "pending")

    def test_day_and_night_sessions_never_share_state(self):
        morning = datetime(2026, 8, 23, 9, 29, tzinfo=TZ)
        daytime = datetime(2026, 8, 23, 9, 30, tzinfo=TZ)
        evening = datetime(2026, 8, 23, 20, 0, tzinfo=TZ)
        night = self.repo.session("42", morning)
        day = self.repo.session("42", daytime)
        next_night = self.repo.session("42", evening)
        self.assertNotEqual(night["session_id"], day["session_id"])
        self.assertNotEqual(day["session_id"], next_night["session_id"])
        self.assertTrue(night["business_date"].endswith(":night"))
        self.assertTrue(day["business_date"].endswith(":day"))

    def test_day_manager_closes_prior_night_waiting_cycle_only(self):
        client_at = datetime(2026, 8, 22, 23, 15, tzinfo=TZ)
        manager_at = datetime(2026, 8, 23, 10, 8, tzinfo=TZ)
        self.repo.upsert_client("42", {"id": 42}, client_at)
        night = self.repo.session("42", client_at)
        cycle_id = self.repo.touch_client_message("42", night["session_id"], client_at)
        day = self.repo.session("42", manager_at)
        self.repo.manager_answer("42", manager_at, 120, event_at=manager_at, session_id=day["session_id"])
        with connect(self.path) as db:
            cycle = db.execute("SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            night_after = db.execute("SELECT * FROM business_sessions WHERE session_id=?", (night["session_id"],)).fetchone()
        self.assertEqual(cycle["status"], "manager_answered")
        self.assertEqual(
            datetime.fromisoformat(cycle["manager_due_at"]),
            datetime(2026, 8, 23, 10, 0, tzinfo=TZ),
        )
        self.assertEqual(cycle["work_response_seconds"], 8 * 60)
        self.assertEqual(night_after["status"], "manager_answered")

    def test_edit_delete_and_bot_messages_use_natural_key_outbox(self):
        self.repo.upsert_connection({"id": "c", "user": {"id": 1}, "rights": {"can_reply": True}}, self.now)
        session = self.repo.session("42", self.now)
        original = {"message_id": 7, "date": int(self.now.timestamp()), "chat": {"id": 42}, "from": {"id": 42}, "text": "old"}
        self.assertTrue(self.repo.save_message("c", original, session["session_id"], "client", self.now, update_id=1))
        claimed = self.repo.outbox_due(self.now)[0]
        self.assertEqual(claimed["entity_id"], "c:42:7")

        edited = dict(original, text="new", edit_date=int((self.now + timedelta(seconds=5)).timestamp()))
        self.assertTrue(self.repo.edit_message("c", edited, self.now + timedelta(seconds=5), update_id=2))
        self.assertFalse(self.repo.outbox_done(claimed["id"], self.now, claimed["lease_token"], claimed["generation"]))
        new_claim = self.repo.outbox_due(self.now + timedelta(seconds=5))[0]
        payload = json.loads(new_claim["payload"])
        self.assertEqual(payload["event_id"], "c:42:7")
        self.assertEqual(payload["text"], "new")
        self.assertEqual(payload["update_id"], "2")
        self.repo.outbox_done(new_claim["id"], self.now, new_claim["lease_token"], new_claim["generation"])

        self.assertEqual(self.repo.mark_deleted_messages("c", "42", [7, 999], self.now), 2)
        with connect(self.path) as db:
            deleted = db.execute("SELECT * FROM business_messages WHERE message_id=7").fetchone()
            tombstone = db.execute("SELECT * FROM business_messages WHERE message_id=999").fetchone()
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(tombstone["message_type"], "deleted")

        self.repo.record_bot_message("c", "42", session["session_id"], 8, "reply", "credit", self.now)
        with connect(self.path) as db:
            count = db.execute("SELECT count(*) FROM sheets_outbox WHERE entity_id='c:42:8'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_outgoing_webhook_row_is_enriched_by_local_bot_record_without_duplicate(self):
        self.repo.upsert_connection(
            {"id": "c", "user": {"id": 100}, "rights": {"can_reply": True}},
            self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        client = {
            "message_id": 70, "date": int(self.now.timestamp()),
            "chat": {"id": 42}, "from": {"id": 42}, "text": "iphone",
        }
        self.repo.save_message(
            "c", client, session["session_id"], "client", self.now,
            update_id=700,
        )
        cycle_id = self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=70,
        )

        bot_at = self.now + timedelta(seconds=2)
        outgoing = {
            "message_id": 71, "date": int(bot_at.timestamp()),
            "chat": {"id": 42}, "from": {"id": 100},
            "sender_business_bot": {"id": 999}, "text": "webhook text",
        }
        self.assertTrue(self.repo.save_message(
            "c", outgoing, None, "business_bot", bot_at + timedelta(seconds=1),
            update_id=701,
        ))
        self.repo.record_bot_message(
            "c", "42", session["session_id"], 71,
            "canonical reply", "product_result",
            bot_at + timedelta(seconds=5), model_query="iphone-16-pro-max",
        )

        with connect(self.path) as db:
            rows = db.execute(
                """SELECT * FROM business_messages
                   WHERE business_connection_id='c' AND chat_id='42'
                   AND message_id=71"""
            ).fetchall()
            outbox_rows = db.execute(
                "SELECT * FROM sheets_outbox WHERE entity_id='c:42:71'"
            ).fetchall()
            cycle = db.execute(
                "SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["update_id"], 701)
        self.assertEqual(rows[0]["session_id"], session["session_id"])
        self.assertEqual(rows[0]["cycle_id"], cycle_id)
        self.assertEqual(rows[0]["text"], "canonical reply")
        self.assertEqual(rows[0]["template_code"], "product_result")
        self.assertEqual(rows[0]["model_query"], "iphone-16-pro-max")
        self.assertEqual(len(outbox_rows), 1)
        outbox_payload = json.loads(outbox_rows[0]["payload"])
        self.assertEqual(outbox_payload["template_code"], "product_result")
        self.assertEqual(outbox_payload["cycle_id"], cycle_id)
        self.assertEqual(datetime.fromisoformat(cycle["first_bot_at"]), bot_at)

    def test_new_outbox_snapshot_preserves_existing_retry_backoff(self):
        self.repo.queue_statistics([["today", "total", 1, self.now.isoformat()]], self.now)
        claimed = self.repo.outbox_due(self.now)[0]
        self.assertTrue(self.repo.outbox_retry(
            claimed["id"], self.now, claimed["attempts"], "Google unavailable",
            claimed["lease_token"], claimed["generation"],
        ))
        with connect(self.path) as db:
            retry_at = datetime.fromisoformat(db.execute(
                "SELECT next_attempt_at FROM sheets_outbox WHERE id=?", (claimed["id"],)
            ).fetchone()[0])
        newer = self.now + timedelta(seconds=1)
        self.repo.queue_statistics([["today", "total", 2, newer.isoformat()]], newer)
        with connect(self.path) as db:
            saved = db.execute("SELECT * FROM sheets_outbox WHERE id=?", (claimed["id"],)).fetchone()
        self.assertEqual(saved["attempts"], claimed["attempts"])
        self.assertEqual(datetime.fromisoformat(saved["next_attempt_at"]), retry_at)
        self.assertEqual(self.repo.outbox_due(newer), [])
        due = self.repo.outbox_due(retry_at)[0]
        self.assertEqual(json.loads(due["payload"])["value"], 2)

    def test_older_edit_and_edit_after_delete_cannot_resurrect_message(self):
        session = self.repo.session("42", self.now)
        original = {"message_id": 9, "date": int(self.now.timestamp()), "chat": {"id": 42}, "text": "old"}
        self.repo.save_message("c", original, session["session_id"], "client", self.now, update_id=1)
        newer_time = self.now + timedelta(seconds=20)
        newer = dict(original, text="newer", edit_date=int(newer_time.timestamp()))
        older = dict(original, text="older", edit_date=int((self.now + timedelta(seconds=10)).timestamp()))
        self.repo.edit_message("c", newer, newer_time, update_id=3)
        self.repo.edit_message("c", older, newer_time, update_id=2)
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM business_messages WHERE message_id=9").fetchone()
        self.assertEqual(row["text"], "newer")
        self.repo.mark_deleted_messages("c", "42", [9], newer_time)
        latest = dict(original, text="resurrected", edit_date=int((newer_time + timedelta(seconds=10)).timestamp()))
        self.repo.edit_message("c", latest, newer_time + timedelta(seconds=10), update_id=4)
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM business_messages WHERE message_id=9").fetchone()
        self.assertEqual(row["text"], "newer")
        self.assertIsNotNone(row["deleted_at"])

    def test_null_legacy_leases_are_recovered(self):
        self.repo.save_update({"update_id": 99, "business_connection": {"id": "c"}}, self.now)
        self.repo.schedule("legacy", "42", "s", "debounce", self.now, {}, self.now)
        self.repo.queue_statistics([["today", "total", 1, self.now.isoformat()]], self.now)
        with connect(self.path) as db:
            db.execute("UPDATE business_updates SET status='running',lease_token=NULL,lease_expires_at=NULL WHERE update_id=99")
            db.execute("UPDATE scheduled_actions SET status='running',lease_token=NULL,lease_expires_at=NULL WHERE dedupe_key='legacy'")
            db.execute("UPDATE sheets_outbox SET status='running',lease_token=NULL,lease_expires_at=NULL")
        recovered = self.repo.recover_stale(self.now)
        self.assertEqual(recovered, {"updates": 1, "actions": 1, "outbox": 1})

    def test_annotation_and_permanent_pause_hooks_are_persistent(self):
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        message = {"message_id": 6, "date": int(self.now.timestamp()), "chat": {"id": 42}, "text": "phone"}
        self.repo.save_message("c", message, session["session_id"], "client", self.now, update_id=6)
        self.assertTrue(self.repo.annotate_message(
            "c", "42", 6, self.now, language="ru", intent="product_search", model_query="phone"
        ))
        self.repo.schedule("debounce:42", "42", session["session_id"], "debounce", self.now, {}, self.now)
        draft = self.repo.get_or_create_business_request(
            "42", session["session_id"], self.now,
        )
        draft_token = self.repo.issue_business_callback(
            draft["request_id"], draft["revision"], "next", {}, self.now,
        )
        self.assertTrue(self.repo.set_bot_paused("42", True, self.now, "active_order"))
        self.assertTrue(self.repo.is_bot_paused("42"))
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM business_messages WHERE message_id=6").fetchone()
            action = db.execute("SELECT * FROM scheduled_actions WHERE dedupe_key='debounce:42'").fetchone()
            paused_draft = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (draft["request_id"],),
            ).fetchone()
            paused_token = db.execute(
                "SELECT * FROM business_callback_tokens WHERE token_hash=?",
                (self.repo._business_callback_hash(draft_token),),
            ).fetchone()
        self.assertEqual((row["language"], row["intent"], row["model_query"]),
                         ("ru", "product_search", "phone"))
        self.assertEqual(action["status"], "cancelled")
        self.assertEqual(paused_draft["status"], "expired")
        self.assertEqual(paused_token["state"], "revoked")

    def test_cycle_metrics_use_telegram_event_dates(self):
        received_client = datetime(2026, 8, 22, 15, 30, tzinfo=TZ)
        sent_client = datetime(2026, 8, 22, 15, 0, tzinfo=TZ)
        received_manager = datetime(2026, 8, 22, 15, 40, tzinfo=TZ)
        sent_manager = datetime(2026, 8, 22, 15, 12, tzinfo=TZ)
        self.repo.upsert_client("42", {"id": 42}, received_client)
        session = self.repo.session("42", sent_client)
        client_message = {"message_id": 1, "date": int(sent_client.timestamp()), "chat": {"id": 42}, "text": "hello"}
        self.repo.save_message("c", client_message, session["session_id"], "client", received_client)
        cycle_id = self.repo.touch_client_message("42", session["session_id"], received_client,
                                                  event_at=sent_client, message_id=1)
        manager_message = {"message_id": 2, "date": int(sent_manager.timestamp()), "chat": {"id": 42}, "text": "answer"}
        self.repo.save_message("c", manager_message, session["session_id"], "manager", received_manager)
        self.repo.manager_answer("42", received_manager, 120, event_at=sent_manager, message_id=2,
                                 session_id=session["session_id"])
        with connect(self.path) as db:
            cycle = db.execute("SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            manager = db.execute("SELECT * FROM business_messages WHERE message_id=2").fetchone()
        self.assertEqual(cycle["calendar_response_seconds"], 12 * 60)
        self.assertEqual(cycle["work_response_seconds"], 12 * 60)
        self.assertEqual(manager["cycle_id"], cycle_id)
        with connect(self.path) as db:
            dialog = db.execute("SELECT * FROM sheets_outbox WHERE entity_type='dialog'").fetchone()
        self.assertIsNotNone(dialog)
        dialog_payload = json.loads(dialog["payload"])
        self.assertEqual(dialog_payload["cycle_id"], cycle_id)
        self.assertEqual(dialog_payload["calendar_response_seconds"], 12 * 60)

    def test_payment_redaction_does_not_corrupt_structured_telegram_ids(self):
        text = "карта 4242 4242 4242 4242, срок 12/29, CVV 123"
        redacted = redact_payment_data(text)
        self.assertNotIn("4242 4242", redacted)
        self.assertNotIn("12/29", redacted)
        self.assertNotIn("CVV 123", redacted)
        self.assertNotIn("8600123412341234", redact_payment_data("8600123412341234"))
        self.assertEqual(redact_payment_data("телефон 998901234567"), "телефон 998901234567")
        self.assertNotIn("12345678901234567890", redact_payment_data("банковский счёт 12345678901234567890"))
        self.assertNotIn("GB82WEST12345698765432", redact_payment_data("IBAN GB82WEST12345698765432"))
        payload = {"update_id": 123456789012345678, "business_message": {
            "message_id": 987654321012345678, "chat": {"id": 555555555555555555}, "text": text}}
        safe = sanitize_telegram_payload(payload)
        self.assertEqual(safe["update_id"], payload["update_id"])
        self.assertEqual(safe["business_message"]["chat"]["id"], payload["business_message"]["chat"]["id"])
        self.assertNotEqual(safe["business_message"]["text"], text)

    def test_scheduler_processes_persisted_update_and_honours_retry_after_contract(self):
        self.assertEqual(retry_after_seconds(RetryableError(23)), 23)
        self.repo.save_update({"update_id": 77, "business_connection": {"id": "c"}}, self.now)

        class Sheets:
            def sync_once(self, now):
                raise AssertionError("disabled in test")

        class Service:
            def __init__(service_self):
                service_self.repo = self.repo
                service_self.settings = SimpleNamespace(sheets_sync_seconds=60)
                service_self.sheets = Sheets()
                service_self.seen = []

            def clock(service_self):
                return self.now

            def process_update(service_self, update):
                service_self.seen.append(update["update_id"])
                service_self.repo.mark_update(update["update_id"], "processed", self.now)

            def execute(service_self, action):
                raise RetryableError(23)

        service = Service()
        self.repo.schedule("credit:42", "42", "s", "credit", self.now, {}, self.now)
        asyncio.run(DurableScheduler(service).run_once(sync_sheets=False))
        self.assertEqual(service.seen, [77])
        self.assertEqual(self.repo.update(77)["status"], "processed")
        with connect(self.path) as db:
            action = db.execute("SELECT * FROM scheduled_actions WHERE dedupe_key='credit:42'").fetchone()
        self.assertEqual(action["status"], "pending")
        self.assertGreaterEqual(datetime.fromisoformat(action["execute_at"]), self.now + timedelta(seconds=23))
        with connect(self.path) as db:
            error = db.execute("SELECT * FROM business_errors ORDER BY error_id DESC LIMIT 1").fetchone()
        self.assertEqual(error["operation"], "credit")

    def test_slow_sheets_cycle_is_non_blocking_and_single_flight(self):
        started = threading.Event()
        release = threading.Event()

        class Sheets:
            def __init__(service_self):
                service_self.calls = 0

            def sync_once(service_self, now, *, clock=None):
                service_self.calls += 1
                started.set()
                release.wait(timeout=5)

        class Service:
            def __init__(service_self):
                service_self.repo = self.repo
                service_self.settings = SimpleNamespace(
                    sheets_sync_seconds=1,
                    night_start=self.now.time(),
                    night_end=(self.now + timedelta(hours=13, minutes=30)).time(),
                )
                service_self.sheets = Sheets()

            def clock(service_self):
                return self.now

            def process_update(service_self, update):
                raise AssertionError("no updates expected")

            def execute(service_self, action):
                raise AssertionError("no actions expected")

        async def scenario():
            service = Service()
            scheduler = DurableScheduler(service)
            with patch("telegram_business.scheduler.statistics_rows", return_value=[]):
                # run_once returns while the remote worker is still blocked.
                await scheduler.run_once()
                self.assertTrue(
                    await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
                )
                first_task = scheduler._sheets_task
                self.assertIsNotNone(first_task)
                self.assertFalse(first_task.done())

                # Even if the interval is due again, the active task is reused.
                scheduler._next_sheets = 0
                await scheduler.run_once()
                self.assertIs(scheduler._sheets_task, first_task)
                self.assertEqual(service.sheets.calls, 1)

                release.set()
                await asyncio.wait_for(first_task, timeout=2)

        asyncio.run(scenario())

    def test_statistics_use_stable_outbox_key(self):
        rows = [{"period": "today", "metric": "total", "value": 1}]
        self.assertEqual(self.repo.queue_statistics(rows, self.now), 1)
        self.assertEqual(self.repo.queue_statistics([{**rows[0], "value": 2}], self.now), 1)
        with connect(self.path) as db:
            saved = db.execute("SELECT * FROM sheets_outbox WHERE entity_type='statistic'").fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["entity_id"], "today:total")
        self.assertEqual(json.loads(saved[0]["payload"])["value"], 2)

    def test_unchanged_statistics_snapshot_stays_synced(self):
        row = {"period": "today", "metric": "total", "value": 7}
        self.assertEqual(self.repo.queue_statistics([row], self.now), 1)
        claimed = self.repo.outbox_due(self.now)[0]
        self.assertTrue(self.repo.outbox_done(
            claimed["id"], self.now, claimed["lease_token"],
            claimed["generation"],
        ))
        with connect(self.path) as db:
            before = db.execute(
                "SELECT * FROM sheets_outbox WHERE id=?", (claimed["id"],)
            ).fetchone()
        later = self.now + timedelta(minutes=1)
        self.assertEqual(self.repo.queue_statistics([row], later), 1)
        with connect(self.path) as db:
            after = db.execute(
                "SELECT * FROM sheets_outbox WHERE id=?", (claimed["id"],)
            ).fetchone()
        self.assertEqual(after["status"], "synced")
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["payload"], before["payload"])
        self.assertEqual(self.repo.outbox_due(later), [])

    def test_outbox_prioritizes_runtime_rows_over_statistics(self):
        self.repo.queue_statistics(
            [{"period": "today", "metric": "total", "value": 1}],
            self.now,
        )
        self.repo.upsert_client("42", {"id": 42}, self.now)
        session = self.repo.session("42", self.now)
        message = {
            "message_id": 1, "date": int(self.now.timestamp()),
            "chat": {"id": 42}, "from": {"id": 42}, "text": "hello",
        }
        self.repo.save_message(
            "c", message, session["session_id"], "client", self.now,
            update_id=1,
        )
        self.repo.touch_client_message(
            "42", session["session_id"], self.now,
            event_at=self.now, message_id=1,
        )
        self.repo.record_error(
            self.now, "test", "operation", RuntimeError("safe")
        )
        due = self.repo.outbox_due(self.now, limit=4)
        self.assertEqual(
            [row["entity_type"] for row in due],
            ["message", "dialog", "error", "statistic"],
        )

    def test_sheets_sync_lease_is_token_fenced_and_recovers_after_expiry(self):
        first = self.repo.acquire_sheets_sync_lease(self.now, lease_seconds=30)
        self.assertTrue(first)
        other = BusinessRepository(self.path)
        self.assertIsNone(
            other.acquire_sheets_sync_lease(
                self.now + timedelta(seconds=29), lease_seconds=30,
            )
        )
        replacement = other.acquire_sheets_sync_lease(
            self.now + timedelta(seconds=31), lease_seconds=30,
        )
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, first)
        self.assertFalse(self.repo.release_sheets_sync_lease(first))
        self.assertTrue(other.release_sheets_sync_lease(replacement))

    def test_request_create_is_single_active_and_restart_safe(self):
        self.repo.upsert_client(
            "42", {"id": 9007199254740993, "first_name": "Client"}, self.now
        )
        session = self.repo.session("42", self.now)
        repositories = [BusinessRepository(self.path) for _ in range(4)]
        barrier = threading.Barrier(len(repositories))
        request_ids = []
        errors = []

        def create(repo):
            try:
                barrier.wait()
                row = repo.get_or_create_business_request(
                    "42",
                    session["session_id"],
                    self.now,
                    business_connection_id="c",
                    business_date=session["business_date"],
                    language="ru",
                    event_at=self.now,
                    message_id=10,
                    telegram_update_id=7000,
                )
                request_ids.append(row["request_id"])
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(repo,)) for repo in repositories]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(request_ids)), 1)
        request_id = request_ids[0]
        self.assertRegex(request_id, r"^[0-9a-f]{32}$")
        restarted = BusinessRepository(self.path)
        self.assertEqual(
            restarted.active_business_request("42")["request_id"], request_id
        )
        self.assertEqual(
            [event["event_type"] for event in restarted.business_request_events(request_id)],
            ["created"],
        )
        with connect(self.path) as db:
            requests = db.execute("SELECT * FROM business_requests").fetchall()
            outbox = db.execute(
                "SELECT * FROM sheets_outbox WHERE entity_type='request'"
            ).fetchall()
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(outbox), 1)
        snapshot = json.loads(outbox[0]["payload"])
        self.assertEqual(snapshot["telegram_user_id"], "9007199254740993")
        self.assertEqual(snapshot["chat_id"], "42")

    def test_request_revision_dynamic_fields_and_safe_outbox(self):
        self.repo.upsert_client("43", {"id": 43}, self.now)
        request = self.repo.get_or_create_business_request("43", None, self.now)
        token = self.repo.issue_business_callback(
            request["request_id"], 1, "select_model", {"choice": 1}, self.now
        )
        full_phone = "+998 90 123 45 67"
        exact_location = "https://maps.google.com/?q=41.311081,69.240562"
        updated = self.repo.update_business_request(
            request["request_id"],
            1,
            self.now + timedelta(seconds=1),
            {
                "state": "review",
                "matched_model": "iPhone 16 Pro Max",
                "attribute_kind": "memory",
                "attribute_value": "256 GB",
                "color": "Black",
                "fulfillment_method": "delivery",
                "phone": full_phone,
                "contact_method": "telegram_contact",
                "location_url": exact_location,
                "address": f"Call {full_phone}, Amir Temur street",
                "price": "14500000",
                "source_updated_at": self.now.isoformat(),
            },
            selections={
                "model_query": "айфон 16 про макс",
                "memory": "256 GB",
                "finish": "Matte",
            },
            event_key="callback:cb-update",
            telegram_update_id=501,
            client_at=self.now + timedelta(seconds=1),
            telegram_message_id=11,
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(json.loads(updated["selection_fields"])["finish"], "Matte")
        self.assertIsNone(
            self.repo.update_business_request(
                request["request_id"], 1, self.now, wizard_state="stale"
            )
        )
        # A generator is accepted and consumed exactly once while removing a
        # dynamic product attribute.
        updated = self.repo.update_business_request(
            request["request_id"],
            2,
            self.now + timedelta(seconds=2),
            clear_selections=(name for name in ("finish",)),
            event_key="callback:cb-clear",
        )
        self.assertNotIn("finish", json.loads(updated["selection_fields"]))
        replay = self.repo.update_business_request(
            request["request_id"],
            2,
            self.now + timedelta(seconds=3),
            wizard_state="ignored-replay",
            event_key="callback:cb-clear",
        )
        self.assertEqual(replay["revision"], 3)
        with connect(self.path) as db:
            token_row = db.execute(
                "SELECT * FROM business_callback_tokens"
            ).fetchone()
            outbox = db.execute(
                "SELECT * FROM sheets_outbox WHERE entity_type='request'"
            ).fetchone()
        self.assertEqual(token_row["state"], "revoked")
        snapshot_text = outbox["payload"]
        snapshot = json.loads(snapshot_text)
        self.assertNotIn(full_phone, snapshot_text)
        self.assertEqual(snapshot["phone_masked"], "***4567")
        self.assertNotIn("41.311081", snapshot_text)
        self.assertNotIn("69.240562", snapshot_text)
        self.assertEqual(snapshot["matched_model"], "iPhone 16 Pro Max")
        self.assertEqual(snapshot["model_query"], "айфон 16 про макс")
        self.assertEqual(snapshot["attribute_kind"], "memory")
        self.assertEqual(snapshot["price_uzs"], "14500000")

    def test_request_completion_rules_cancel_privacy_and_expiry(self):
        delivery = self.repo.get_or_create_business_request("44", None, self.now)
        with self.assertRaisesRegex(ValueError, "invalid_phone"):
            self.repo.update_business_request(
                delivery["request_id"], 1, self.now, phone="8600123412341234"
            )
        with self.assertRaisesRegex(ValueError, "phone_required"):
            self.repo.complete_business_request(
                delivery["request_id"], "complete_delivery", 1, self.now
            )
        delivery = self.repo.update_business_request(
            delivery["request_id"],
            1,
            self.now,
            phone="+998901112233",
            exact_model="iPhone 16 Pro Max",
        )
        with self.assertRaisesRegex(ValueError, "location_required"):
            self.repo.complete_business_request(
                delivery["request_id"], "complete_delivery", 2, self.now
            )
        delivery = self.repo.update_business_request(
            delivery["request_id"], 2, self.now, address="Tashkent, Amir Temur 1"
        )
        submitted = self.repo.complete_business_request(
            delivery["request_id"],
            "complete_delivery",
            3,
            self.now,
            event_key="complete:44",
        )
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(submitted["wizard_state"], "complete_delivery")
        self.assertEqual(
            self.repo.complete_business_request(
                delivery["request_id"],
                "complete_delivery",
                3,
                self.now,
                event_key="complete:44",
            )["revision"],
            submitted["revision"],
        )
        self.assertIsNone(
            self.repo.expire_business_request(
                delivery["request_id"], submitted["revision"], self.now
            )
        )

        pickup = self.repo.get_or_create_business_request("45", None, self.now)
        with self.assertRaisesRegex(ValueError, "model_required"):
            self.repo.complete_business_request(
                pickup["request_id"], "complete_pickup", 1, self.now
            )
        pickup = self.repo.update_business_request(
            pickup["request_id"], 1, self.now, exact_model="AirPods Pro 2"
        )
        pickup = self.repo.complete_business_request(
            pickup["request_id"], "complete_pickup", 2, self.now
        )
        self.assertEqual(pickup["fulfillment_method"], "pickup")
        self.assertIsNone(pickup["phone"])
        self.assertIsNone(pickup["location_url"])

        draft = self.repo.get_or_create_business_request("46", None, self.now)
        draft = self.repo.update_business_request(
            draft["request_id"],
            1,
            self.now,
            phone="+998909998877",
            location_url="https://maps.google.com/?q=41.123456,69.123456",
            address="41.123456, 69.123456",
        )
        cancelled = self.repo.cancel_business_request(
            draft["request_id"], 2, self.now
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["needs_manager_reply"], 0)
        self.assertIsNone(cancelled["phone"])
        self.assertIsNone(cancelled["location_url"])
        self.assertIsNone(cancelled["address"])
        with connect(self.path) as db:
            cancel_snapshot = db.execute(
                "SELECT payload FROM sheets_outbox WHERE entity_type='request' AND entity_id=?",
                (draft["request_id"],),
            ).fetchone()["payload"]
        self.assertNotIn("998909998877", cancel_snapshot)
        self.assertNotIn("41.123456", cancel_snapshot)
        original_retry = self.repo.get_or_create_business_request(
            "46", None, self.now + timedelta(minutes=1), telegram_update_id=7460
        )
        # This first call has a different origin and therefore legitimately
        # starts the next request after cancellation.
        self.assertNotEqual(original_retry["request_id"], draft["request_id"])
        original_retry = self.repo.cancel_business_request(
            original_retry["request_id"], 1, self.now + timedelta(minutes=1)
        )
        self.assertEqual(
            self.repo.get_or_create_business_request(
                "46",
                None,
                self.now + timedelta(hours=1),
                telegram_update_id=7460,
            )["request_id"],
            original_retry["request_id"],
        )

        expiring = self.repo.get_or_create_business_request("47", None, self.now)
        expiring_token = self.repo.issue_business_callback(
            expiring["request_id"], 1, "next", {}, self.now
        )
        expired = self.repo.expire_business_request(
            expiring["request_id"], 1, self.now + timedelta(hours=13)
        )
        self.assertEqual(expired["status"], "expired")
        receipt = self.repo.consume_business_callback(
            expiring_token, "expired-callback", "47", self.now + timedelta(hours=13)
        )
        self.assertEqual(receipt["outcome"], "stale")
        manager_at = self.now + timedelta(hours=14)
        self.repo.manager_answer(
            "47", manager_at, 120, event_at=manager_at, message_id=900
        )
        self.assertEqual(
            self.repo.business_request(expiring["request_id"])["status"],
            "manager_closed",
        )

        claimed = self.repo.get_or_create_business_request(
            "471", None, self.now,
        )
        claimed_token = self.repo.issue_business_callback(
            claimed["request_id"], 1, "next", {}, self.now,
            ttl_seconds=14 * 60 * 60,
        )
        callback_at = self.now + timedelta(hours=12, minutes=59)
        claimed_receipt = self.repo.consume_business_callback(
            claimed_token, "claimed-before-night-end", "471", callback_at,
        )
        self.assertEqual(claimed_receipt["status"], "claimed")
        with self.assertRaises(PendingBusinessCallbackError):
            self.repo.expire_business_request(
                claimed["request_id"], 1, self.now + timedelta(hours=13),
            )
        self.assertTrue(
            self.repo.finish_business_callback(
                "claimed-before-night-end", callback_at, applied=True,
            )
        )
        expired_after_callback = self.repo.expire_business_request(
            claimed["request_id"], 1, self.now + timedelta(hours=13),
        )
        self.assertEqual(expired_after_callback["status"], "expired")

        orphan = self.repo.get_or_create_business_request(
            "4711", None, self.now,
        )
        orphan_token = self.repo.issue_business_callback(
            orphan["request_id"], 1, "next", {}, self.now,
            ttl_seconds=14 * 60 * 60,
        )
        self.assertEqual(
            self.repo.consume_business_callback(
                orphan_token, "orphaned-claim", "4711", self.now,
            )["status"],
            "claimed",
        )
        orphan_expired = self.repo.expire_business_request(
            orphan["request_id"], 1, self.now + timedelta(minutes=6),
        )
        self.assertEqual(orphan_expired["status"], "expired")
        self.assertEqual(
            self.repo.business_callback_receipt("orphaned-claim")["outcome"],
            "request_expired",
        )

    def test_request_submit_projects_session_atomically_before_manager_answer(self):
        self.repo.upsert_client("472", {"id": 472}, self.now)
        session = self.repo.session("472", self.now)
        request = self.repo.get_or_create_business_request(
            "472",
            session["session_id"],
            self.now,
            event_at=self.now,
            message_id=1,
        )
        request = self.repo.update_business_request(
            request["request_id"],
            request["revision"],
            self.now,
            exact_model="AirPods Pro 2",
            option_value="USB-C",
            color_any=1,
        )

        submitted = self.repo.complete_business_request(
            request["request_id"],
            "complete_pickup",
            request["revision"],
            self.now,
        )
        projected = self.repo.session_by_id(session["session_id"])
        self.assertEqual(projected["status"], "waiting_manager")
        self.assertEqual(projected["needs_manager_reply"], 1)
        self.assertEqual(projected["matched_model"], "AirPods Pro 2")
        self.assertEqual(projected["memory"], "USB-C")
        self.assertEqual(projected["color"], "any")

        manager_at = self.now + timedelta(minutes=1)
        self.repo.manager_answer(
            "472",
            manager_at,
            120,
            event_at=manager_at,
            message_id=2,
            session_id=session["session_id"],
        )
        answered = self.repo.session_by_id(session["session_id"])
        self.assertEqual(answered["status"], "manager_answered")
        self.assertEqual(answered["needs_manager_reply"], 0)
        self.assertEqual(
            self.repo.business_request(submitted["request_id"])["status"],
            "manager_closed",
        )

    def test_pending_callback_keeps_expiry_retryable_and_terminal_update_wakes_it(self):
        request = self.repo.get_or_create_business_request(
            "473", None, self.now, business_connection_id="c",
        )
        token = self.repo.issue_business_callback(
            request["request_id"], 1, "next", {}, self.now,
            ttl_seconds=3600,
        )
        callback = {
            "update_id": 7473,
            "callback_query": {
                "id": "callback-expiry-owner",
                "from": {"id": 473},
                "data": f"nr1:{token}",
                "message": {
                    "message_id": 10,
                    "business_connection_id": "c",
                    "chat": {"id": 473, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                callback, self.now, allowed_connection_id="c",
            )
        )
        claimed_update = self.repo.claim_due_updates(self.now, limit=1)[0]
        self.assertEqual(
            self.repo.consume_business_callback(
                token, "callback-expiry-owner", "473", self.now,
            )["status"],
            "claimed",
        )
        self.repo.schedule(
            f"request-expire:{request['request_id']}",
            "473",
            None,
            "request_expire",
            self.now,
            {"request_id": request["request_id"], "connection_id": "c"},
            self.now,
        )
        with connect(self.path) as db:
            db.execute(
                """UPDATE scheduled_actions SET attempts=7
                   WHERE dedupe_key=?""",
                (f"request-expire:{request['request_id']}",),
            )

        class PendingService:
            repo = self.repo

            @staticmethod
            def clock():
                return self.now

            @staticmethod
            def execute(_action):
                raise PendingBusinessCallbackError("callback still live")

        asyncio.run(DurableScheduler(PendingService())._process_actions(self.now))
        with connect(self.path) as db:
            retrying_expiry = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key=?",
                (f"request-expire:{request['request_id']}",),
            ).fetchone()
            db.execute(
                """UPDATE business_updates SET attempts=12,status='running',
                   lease_token=? WHERE update_id=?""",
                (claimed_update["lease_token"], 7473),
            )
        self.assertEqual(retrying_expiry["status"], "pending")
        self.assertEqual(retrying_expiry["attempts"], 8)

        self.assertTrue(
            self.repo.retry_update(
                7473,
                self.now + timedelta(minutes=10),
                "permanent callback edit failure",
                lease_token=claimed_update["lease_token"],
                max_attempts=12,
            )
        )
        with connect(self.path) as db:
            terminal_update = db.execute(
                "SELECT status FROM business_updates WHERE update_id=7473",
            ).fetchone()
            terminal_receipt = db.execute(
                """SELECT status,outcome FROM business_callback_receipts
                   WHERE callback_query_id='callback-expiry-owner'""",
            ).fetchone()
            woken_expiry = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key=?",
                (f"request-expire:{request['request_id']}",),
            ).fetchone()
        self.assertEqual(terminal_update["status"], "failed")
        self.assertEqual(
            (terminal_receipt["status"], terminal_receipt["outcome"]),
            ("rejected", "update_failed"),
        )
        self.assertEqual(woken_expiry["status"], "pending")
        self.assertEqual(woken_expiry["attempts"], 0)

        # A callback failing before the untouched night boundary must not pull
        # the request-expire action forward from 09:30.
        future = self.repo.get_or_create_business_request(
            "474", None, self.now, business_connection_id="c",
        )
        future_token = self.repo.issue_business_callback(
            future["request_id"], 1, "next", {}, self.now,
            ttl_seconds=3600,
        )
        future_callback = {
            "update_id": 7474,
            "callback_query": {
                "id": "callback-before-boundary",
                "from": {"id": 474},
                "data": f"nr1:{future_token}",
                "message": {
                    "message_id": 11,
                    "business_connection_id": "c",
                    "chat": {"id": 474, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                future_callback, self.now, allowed_connection_id="c",
            )
        )
        future_claim = self.repo.claim_due_updates(self.now, limit=1)[0]
        self.repo.consume_business_callback(
            future_token, "callback-before-boundary", "474", self.now,
        )
        original_boundary = self.now + timedelta(hours=13, minutes=30)
        self.repo.schedule(
            f"request-expire:{future['request_id']}",
            "474",
            None,
            "request_expire",
            original_boundary,
            {"request_id": future["request_id"], "connection_id": "c"},
            self.now,
        )
        with connect(self.path) as db:
            db.execute(
                """UPDATE business_updates SET attempts=12,status='running',
                   lease_token=? WHERE update_id=7474""",
                (future_claim["lease_token"],),
            )
        self.repo.retry_update(
            7474,
            self.now + timedelta(minutes=90),
            "permanent callback failure before boundary",
            lease_token=future_claim["lease_token"],
            max_attempts=12,
        )
        with connect(self.path) as db:
            untouched = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key=?",
                (f"request-expire:{future['request_id']}",),
            ).fetchone()
        self.assertEqual(untouched["status"], "pending")
        self.assertEqual(untouched["attempts"], 0)
        self.assertEqual(
            datetime.fromisoformat(untouched["execute_at"]), original_boundary,
        )

        # Once the boundary action has attempted, completing the callback
        # should wake its delayed retry immediately instead of waiting for the
        # exponential backoff slot.
        applied = self.repo.get_or_create_business_request("475", None, self.now)
        applied_token = self.repo.issue_business_callback(
            applied["request_id"], 1, "next", {}, self.now,
        )
        self.repo.consume_business_callback(
            applied_token, "callback-applied-after-boundary", "475", self.now,
        )
        self.repo.schedule(
            f"request-expire:{applied['request_id']}",
            "475",
            None,
            "request_expire",
            self.now,
            {"request_id": applied["request_id"], "connection_id": "c"},
            self.now,
        )
        with connect(self.path) as db:
            db.execute(
                """UPDATE scheduled_actions SET attempts=1,execute_at=?,
                   next_attempt_at=?,last_error='callback still live'
                   WHERE dedupe_key=?""",
                (
                    (self.now + timedelta(hours=1)).isoformat(),
                    (self.now + timedelta(hours=1)).isoformat(),
                    f"request-expire:{applied['request_id']}",
                ),
            )
        finished_at = self.now + timedelta(minutes=10)
        self.assertTrue(
            self.repo.finish_business_callback(
                "callback-applied-after-boundary", finished_at, applied=True,
            )
        )
        with connect(self.path) as db:
            immediately_due = db.execute(
                "SELECT * FROM scheduled_actions WHERE dedupe_key=?",
                (f"request-expire:{applied['request_id']}",),
            ).fetchone()
        self.assertEqual(immediately_due["status"], "pending")
        self.assertEqual(immediately_due["attempts"], 0)
        self.assertEqual(
            datetime.fromisoformat(immediately_due["execute_at"]), finished_at,
        )

    def test_callback_tokens_are_revisioned_atomic_and_replayable(self):
        request = self.repo.get_or_create_business_request("48", None, self.now)
        token = self.repo.issue_business_callback(
            request["request_id"], 1, "select_color", {"color": "Black"}, self.now
        )
        self.assertTrue(token)
        with connect(self.path) as db:
            stored = db.execute("SELECT * FROM business_callback_tokens").fetchone()
        self.assertNotEqual(stored["token_hash"], token)
        self.assertNotIn(token, stored["payload"])

        wrong = self.repo.consume_business_callback(token, "cb-wrong", "999", self.now)
        self.assertEqual(wrong["status"], "rejected")
        self.assertEqual(wrong["outcome"], "wrong_chat")
        accepted = self.repo.consume_business_callback(
            f"nr1:{token}", "cb-good", "48", self.now
        )
        self.assertEqual(accepted["status"], "claimed")
        self.assertEqual(accepted["outcome"], "accepted")
        self.assertEqual(accepted["action"], "select_color")
        self.assertEqual(json.loads(accepted["payload"]), {"color": "Black"})
        self.assertTrue(
            self.repo.finish_business_callback(
                "cb-good",
                self.now,
                applied=True,
                result_revision=2,
                result={"state": "fulfillment"},
            )
        )

        restarted = BusinessRepository(self.path)
        replay = restarted.consume_business_callback(token, "cb-good", "48", self.now)
        self.assertEqual(replay["status"], "applied")
        self.assertEqual(replay["action"], "select_color")
        self.assertEqual(json.loads(replay["result"]), {"state": "fulfillment"})
        other_query = restarted.consume_business_callback(
            token, "cb-other", "48", self.now
        )
        self.assertEqual(other_query["outcome"], "already_used")

        click_request = self.repo.get_or_create_business_request("481", None, self.now)
        click_token = self.repo.issue_business_callback(
            click_request["request_id"], 1, "next", {}, self.now
        )
        click_at = self.now + timedelta(minutes=5)
        self.assertEqual(
            self.repo.consume_business_callback(
                click_token, "cb-click", "481", click_at
            )["outcome"],
            "accepted",
        )
        # Even before service applies the claimed action, its durable receipt
        # and request watermark fence an older manager event.
        self.repo.manager_answer(
            "481",
            click_at + timedelta(minutes=1),
            120,
            event_at=self.now + timedelta(minutes=2),
            message_id=10,
        )
        self.assertEqual(
            self.repo.business_request(click_request["request_id"])["status"],
            "collecting",
        )

        delayed_request = self.repo.get_or_create_business_request("482", None, self.now)
        timely_token = self.repo.issue_business_callback(
            delayed_request["request_id"], 1, "next", {}, self.now, ttl_seconds=60
        )
        timely_callback = {
            "update_id": 7482,
            "callback_query": {
                "id": "cb-timely-delayed",
                "from": {"id": 482},
                "data": timely_token,
                "message": {
                    "message_id": 1,
                    "business_connection_id": "c",
                    "chat": {"id": 482, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                timely_callback,
                self.now + timedelta(seconds=30),
                allowed_connection_id="c",
            )
        )
        # Expiration is evaluated at durable webhook receipt time, so downtime
        # does not invalidate a click Telegram delivered before the deadline.
        delayed_receipt = self.repo.consume_business_callback(
            timely_token,
            "cb-timely-delayed",
            "482",
            self.now + timedelta(minutes=2),
        )
        self.assertEqual(delayed_receipt["outcome"], "accepted")
        self.assertEqual(
            delayed_receipt["created_at"],
            (self.now + timedelta(seconds=30)).isoformat(),
        )

        actor_request = self.repo.get_or_create_business_request("483", None, self.now)
        actor_token = self.repo.issue_business_callback(
            actor_request["request_id"], 1, "next", {}, self.now
        )
        manager_click = {
            "update_id": 7483,
            "callback_query": {
                "id": "cb-manager-click",
                "from": {"id": 999999},
                "data": f"nr1:{actor_token}",
                "message": {
                    "message_id": 2,
                    "business_connection_id": "c",
                    "chat": {"id": 483, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                manager_click, self.now, allowed_connection_id="c"
            )
        )
        # The stored callback actor wins over a caller-supplied message chat id.
        rejected_actor = self.repo.consume_business_callback(
            actor_token, "cb-manager-click", "483", self.now
        )
        self.assertEqual(rejected_actor["outcome"], "wrong_chat")
        self.assertEqual(rejected_actor["chat_id"], "999999")

        connection_request = self.repo.get_or_create_business_request(
            "484", None, self.now, business_connection_id="c"
        )
        connection_token = self.repo.issue_business_callback(
            connection_request["request_id"], 1, "next", {}, self.now
        )
        mismatched_connection = {
            "update_id": 7484,
            "callback_query": {
                "id": "cb-wrong-connection",
                "from": {"id": 484},
                "data": f"nr1:{connection_token}",
                "message": {
                    "message_id": 3,
                    "business_connection_id": "other",
                    "chat": {"id": 484, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                mismatched_connection, self.now, allowed_connection_id="c"
            )
        )
        self.assertEqual(
            self.repo.consume_business_callback(
                connection_token, "cb-wrong-connection", "484", self.now
            )["outcome"],
            "wrong_connection",
        )
        self.repo.upsert_connection(
            {"id": "disabled", "is_enabled": False,
             "rights": {"can_reply": False}},
            self.now,
        )
        disabled_request = self.repo.get_or_create_business_request(
            "485", None, self.now, business_connection_id="disabled"
        )
        disabled_token = self.repo.issue_business_callback(
            disabled_request["request_id"], 1, "next", {}, self.now
        )
        self.assertEqual(
            self.repo.consume_business_callback(
                disabled_token, "cb-disabled", "485", self.now
            )["outcome"],
            "connection_disabled",
        )

        # A delayed worker must evaluate the manager lock at the durable
        # callback webhook time. A click received inside the lock cannot become
        # valid merely because processing resumed after the lock expired.
        self.repo.upsert_client("487", {"id": 487}, self.now)
        locked_request = self.repo.get_or_create_business_request(
            "487", None, self.now, business_connection_id="c"
        )
        locked_token = self.repo.issue_business_callback(
            locked_request["request_id"], 1, "next", {}, self.now,
            ttl_seconds=3 * 60 * 60,
        )
        lock_until = self.now + timedelta(minutes=121)
        callback_received_at = self.now + timedelta(minutes=61)
        with connect(self.path) as db:
            db.execute(
                "UPDATE business_clients SET manager_lock_until=? WHERE chat_id=?",
                (lock_until.isoformat(), "487"),
            )
        locked_callback = {
            "update_id": 7487,
            "callback_query": {
                "id": "cb-delayed-inside-lock",
                "from": {"id": 487},
                "data": f"nr1:{locked_token}",
                "message": {
                    "message_id": 4,
                    "business_connection_id": "c",
                    "chat": {"id": 487, "type": "private"},
                },
            },
        }
        self.assertTrue(
            self.repo.save_update(
                locked_callback,
                callback_received_at,
                allowed_connection_id="c",
            )
        )
        delayed_locked_receipt = self.repo.consume_business_callback(
            locked_token,
            "cb-delayed-inside-lock",
            "487",
            self.now + timedelta(minutes=122),
        )
        self.assertEqual(delayed_locked_receipt["outcome"], "manager_locked")

        self.repo.upsert_client("486", {"id": 486}, self.now)
        paused_request = self.repo.get_or_create_business_request("486", None, self.now)
        paused_token = self.repo.issue_business_callback(
            paused_request["request_id"], 1, "next", {}, self.now
        )
        self.assertTrue(
            self.repo.set_bot_paused("486", True, self.now, "staff")
        )
        self.assertEqual(
            self.repo.consume_business_callback(
                paused_token, "cb-paused", "486", self.now
            )["outcome"],
            "stale",
        )
        self.assertEqual(
            self.repo.business_request(paused_request["request_id"])["status"],
            "expired",
        )

    def test_callback_update_rows_are_durable_and_query_id_idempotent(self):
        def update(update_id, callback_id):
            return {
                "update_id": update_id,
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": 49},
                    "data": "brq:opaque",
                    "message": {
                        "message_id": 700,
                        "business_connection_id": "c",
                        "chat": {"id": 49, "type": "private"},
                    },
                },
            }

        self.assertTrue(
            self.repo.save_update(update(7001, "query-1"), self.now,
                                  allowed_connection_id="c")
        )
        self.assertTrue(
            self.repo.save_update(update(7002, "query-2"), self.now,
                                  allowed_connection_id="c")
        )
        self.assertFalse(
            self.repo.save_update(update(7003, "query-1"), self.now,
                                  allowed_connection_id="c")
        )
        claimed = self.repo.claim_due_updates(self.now)
        self.assertEqual([row["update_id"] for row in claimed], [7001, 7002])
        self.assertTrue(all(row["event_type"] == "callback_query" for row in claimed))
        self.assertTrue(all(row["message_id"] is None for row in claimed))
        self.assertTrue(all(row["callback_message_id"] == 700 for row in claimed))
        self.assertEqual(
            {row["callback_query_id"] for row in claimed}, {"query-1", "query-2"}
        )

    def test_manager_answer_atomically_closes_only_covered_request(self):
        covered = self.repo.get_or_create_business_request(
            "50", None, self.now, event_at=self.now, message_id=10
        )
        covered_token = self.repo.issue_business_callback(
            covered["request_id"], 1, "next", {}, self.now
        )
        answer_at = self.now + timedelta(minutes=1)
        self.repo.manager_answer(
            "50", answer_at, 120, event_at=answer_at, message_id=20
        )
        closed = self.repo.business_request(covered["request_id"])
        self.assertEqual(closed["status"], "manager_closed")
        self.assertEqual(closed["needs_manager_reply"], 0)
        self.assertEqual(closed["closed_by_message_id"], 20)
        self.assertEqual(
            self.repo.consume_business_callback(
                covered_token, "covered-token", "50", answer_at
            )["outcome"],
            "stale",
        )

        newer = self.repo.get_or_create_business_request(
            "50",
            None,
            answer_at + timedelta(minutes=2),
            event_at=answer_at + timedelta(minutes=2),
            message_id=30,
        )
        newer_token = self.repo.issue_business_callback(
            newer["request_id"],
            1,
            "next",
            {},
            answer_at + timedelta(minutes=2),
            ttl_seconds=3 * 3600,
        )
        # A delayed, older manual webhook must not close or revoke a request
        # whose latest customer activity is newer than that answer.
        self.repo.manager_answer(
            "50",
            answer_at + timedelta(minutes=3),
            120,
            event_at=answer_at + timedelta(minutes=1),
            message_id=21,
        )
        self.assertEqual(
            self.repo.business_request(newer["request_id"])["status"], "collecting"
        )
        valid = self.repo.consume_business_callback(
            newer_token, "newer-token", "50", answer_at + timedelta(minutes=123)
        )
        self.assertEqual(valid["outcome"], "accepted")

        # Crash window: the raw/client message has been committed but the wizard
        # request metadata has not yet been advanced by service processing.
        session = self.repo.session("51", self.now)
        crash_window = self.repo.get_or_create_business_request(
            "51", session["session_id"], self.now, event_at=self.now, message_id=1
        )
        later_client_at = self.now + timedelta(minutes=5)
        self.repo.save_message(
            "c",
            {
                "message_id": 2,
                "date": int(later_client_at.timestamp()),
                "chat": {"id": 51},
                "from": {"id": 51},
                "text": "+998901234567",
            },
            session["session_id"],
            "client",
            later_client_at,
        )
        self.repo.manager_answer(
            "51",
            later_client_at + timedelta(minutes=1),
            120,
            event_at=self.now + timedelta(minutes=2),
            message_id=3,
        )
        self.assertEqual(
            self.repo.business_request(crash_window["request_id"])["status"],
            "collecting",
        )


if __name__ == "__main__":
    unittest.main()
