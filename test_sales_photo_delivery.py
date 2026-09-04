from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import OrderRepository

from sales_photo_bot.delivery import (
    DeliveryReader,
    DeliverySalesBridge,
    normalize_delivery_block,
)
from sales_photo_bot.config import Settings
from sales_photo_bot.phones import extract_caption_phones
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import SalesPhotoService


CARD = (
    "\u2063\u2063🆔: 1\n\n"
    "🛒💵:A30 Umiddan\n"
    "rasxod:170$\n\n"
    "📞: +998 90 123 45 67\n\n"
    "Наличка\n💵:\n🇺🇿:\n\n"
    "Card/Terminal/Paynet\n💵:\n🇺🇿:"
)
CHAT_ID = -1001234567890
TOKEN = "1234567890:" + "A" * 35


def settings(root: Path, delivery_path: Path) -> Settings:
    return Settings(
        bot_token=TOKEN,
        chat_id=CHAT_ID,
        db_path=root / "sales.db",
        heartbeat_path=root / "heartbeat",
        delete_retry_seconds=1,
        source_edit_grace_seconds=1,
        startup_drain_seconds=1,
        delivery_db_path=delivery_path,
    )


def create_delivery_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE orders(
                id INTEGER PRIMARY KEY,
                order_number INTEGER,
                client_phone TEXT,
                client_phone_2 TEXT,
                manager_name TEXT,
                seller_name TEXT,
                assigned_courier_name TEXT,
                courier_name TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                delivered_at TEXT
            );
            CREATE TABLE order_events(
                id INTEGER PRIMARY KEY,
                order_id INTEGER,
                event_type TEXT,
                changed_fields TEXT
            );
            """
        )


class DeliveryReaderTests(unittest.TestCase):
    def test_reads_event_cursor_from_base_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.db"
            create_delivery_db(path)
            with sqlite3.connect(path) as db:
                db.executemany(
                    "INSERT INTO order_events VALUES(?,?,?,?)",
                    (
                        (1, 10, "order_created", '["product","client_phone"]'),
                        (2, 10, "order_updated", "invalid-json"),
                    ),
                )

            reader = DeliveryReader(path)

            self.assertEqual(reader.latest_event_id(), 2)
            events = reader.events_after(0)
            self.assertEqual([event.id for event in events], [1, 2])
            self.assertEqual(events[0].changed_fields, {"product", "client_phone"})
            self.assertEqual(events[1].changed_fields, frozenset())

    def test_one_delivery_matches_many_sales_cards_but_two_match_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.db"
            create_delivery_db(path)
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Abbos",
                        "Muzrob Oka",
                        None,
                        "on_way",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T08:30:00+00:00",
                        None,
                    ),
                )
                db.commit()
            index = DeliveryReader(path).index(
                date(2026, 9, 1), date(2026, 9, 1)
            )

            first = index.match(("+998 90 123 45 67",), date(2026, 9, 1))
            second = index.match(("901234567",), date(2026, 9, 1))

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(first.courier_name, "Muzrob Oka")

            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(2,12,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Ali",
                        "Courier 2",
                        None,
                        "pending",
                        "2026-09-01T09:00:00+00:00",
                        "2026-09-01T09:00:00+00:00",
                        None,
                    ),
                )
                db.commit()
            ambiguous = DeliveryReader(path).index(
                date(2026, 9, 1), date(2026, 9, 1)
            )
            self.assertEqual(
                ambiguous.match_count(("+998 90 123 45 67",), date(2026, 9, 1)),
                2,
            )
            self.assertIsNone(
                ambiguous.match(("+998 90 123 45 67",), date(2026, 9, 1))
            )

    def test_completed_uses_actual_tashkent_time_and_eta_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.db"
            create_delivery_db(path)
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Abbos",
                        None,
                        "Muzrob Oka",
                        "completed",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                    ),
                )
                db.commit()
            order = DeliveryReader(path).index(
                date(2026, 9, 1), date(2026, 9, 1)
            ).orders[0]
            normalized = normalize_delivery_block(CARD, (), order, max_length=1024)

            self.assertIn("Доставка: Muzrob Oka", normalized.body)
            self.assertIn("Статус: ✅ Доставлено (18:42)", normalized.body)
            self.assertNotIn("Будет", normalized.body)

    def test_resolve_returns_the_phone_that_actually_matched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.db"
            create_delivery_db(path)
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998917654321",
                        None,
                        "Texnikach",
                        "Abbos",
                        "Muzrob Oka",
                        None,
                        "on_way",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T08:30:00+00:00",
                        None,
                    ),
                )
                db.commit()
            index = DeliveryReader(path).index(
                date(2026, 9, 1), date(2026, 9, 1)
            )

            count, order, matched_phone = index.resolve(
                ("+998 90 123 45 67", "+998 91 765 43 21"),
                date(2026, 9, 1),
            )

            self.assertEqual(count, 1)
            self.assertIsNotNone(order)
            self.assertEqual(matched_phone, "+998 91 765 43 21")
            self.assertEqual(
                index.resolve(
                    ("+998 91 765 43 21",),
                    date(2026, 9, 2),
                ),
                (0, None, None),
            )

            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(2,12,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Ali",
                        "Courier 2",
                        None,
                        "pending",
                        "2026-09-01T09:00:00+00:00",
                        "2026-09-01T09:00:00+00:00",
                        None,
                    ),
                )
                db.commit()
            ambiguous = DeliveryReader(path).index(
                date(2026, 9, 1), date(2026, 9, 1)
            )

            self.assertEqual(
                ambiguous.resolve(
                    ("+998 90 123 45 67", "+998 91 765 43 21"),
                    date(2026, 9, 1),
                ),
                (2, None, None),
            )


class DeliveryCardTests(unittest.TestCase):
    def test_extracts_only_phone_field_and_removes_delivery_block(self):
        self.assertEqual(
            extract_caption_phones(CARD),
            ("+998 90 123 45 67",),
        )
        with_block = CARD + "\n\nДоставка: Muzrob Oka\nСтатус: 🚗 Курьер едет"
        removed = normalize_delivery_block(with_block, (), None, max_length=1024)
        self.assertEqual(removed.body, CARD)

    def test_unmatched_card_is_byte_for_byte_unchanged(self):
        body = CARD + "  \n"
        untouched = normalize_delivery_block(body, (), None, max_length=1024)
        self.assertEqual(untouched.body, body)
        self.assertFalse(untouched.changed)


class DeliveryManagerTests(unittest.TestCase):
    def _linked_repository(self, root: Path) -> SalesPhotoRepository:
        repo = SalesPhotoRepository(root / "sales.db")
        repo.claim_photo(
            CHAT_ID,
            10,
            "unique",
            source_file_id="file",
            sale_date=date(2026, 9, 1),
        )
        repo.mark_reposted(CHAT_ID, 10, 200)
        repo.mark_complete(CHAT_ID, 10)
        repo.upsert_delivery_link(
            CHAT_ID,
            10,
            delivery_order_id=1,
            delivery_order_number=11,
            matched_phone="+998 90 123 45 67",
            sale_date=date(2026, 9, 1),
        )
        return repo

    def test_manual_manager_wins_over_later_delivery_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self._linked_repository(Path(directory))
            repo.sync_auto_delivery_manager(CHAT_ID, 10, "Abbos")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 1))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 1)
            )
            self.assertTrue(repo.mark_delivery_manager_manual(CHAT_ID, 200))

            _, changed = repo.sync_auto_delivery_manager(CHAT_ID, 10, "Otabek")

            self.assertFalse(changed)
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")

    def test_missing_delivery_manager_clears_only_automatic_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self._linked_repository(Path(directory))
            repo.sync_auto_delivery_manager(CHAT_ID, 10, "Abbos")

            _, changed = repo.sync_auto_delivery_manager(CHAT_ID, 10, None)

            self.assertTrue(changed)
            self.assertIsNone(repo.selected_manager(CHAT_ID, 200))


class DeliveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_edited_multiline_card_triggers_immediate_delivery_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            create_delivery_db(delivery_path)
            with sqlite3.connect(delivery_path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Otabek",
                        "Muzrob Oka",
                        None,
                        "on_way",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T08:30:00+00:00",
                        None,
                    ),
                )
                db.commit()
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            message = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=200,
                caption=(
                    "\u2063\u2063🆔: 1\n\n"
                    "🛒💵: Supplier 170$\nGift\nA33 8/256\n"
                    "rasxod:\n\n📞:901234567\n\n"
                    "Наличка\n💵:\n🇺🇿:\n\n"
                    "Card/Terminal/Paynet\n💵:\n🇺🇿:"
                ),
                caption_entities=(),
                text=None,
                entities=(),
                reply_markup=None,
            )
            bot = SimpleNamespace(
                edit_message_caption=AsyncMock(return_value=True),
                edit_message_text=AsyncMock(),
            )
            service = SalesPhotoService(settings(root, delivery_path), repo)

            await service.on_edited_photo(
                SimpleNamespace(edited_channel_post=message, update_id=50),
                SimpleNamespace(bot=bot),
            )

            self.assertEqual(len(repo.delivery_links_for_orders(CHAT_ID, {1})), 1)
            caption = bot.edit_message_caption.await_args.kwargs["caption"]
            self.assertIn("📞: +998 90 123 45 67", caption)
            self.assertIn("Доставка: Muzrob Oka", caption)
            self.assertIn("Статус: 🚗 Курьер едет", caption)

    async def test_multiline_supplier_card_is_linked_without_overwriting_manual_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            create_delivery_db(delivery_path)
            with sqlite3.connect(delivery_path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(124,124,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Otabek",
                        "Muzrob Oka",
                        None,
                        "completed",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                    ),
                )
                db.commit()
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(
                    CHAT_ID,
                    200,
                    "Otabek",
                    0,
                )
            )
            cards = {
                200: (
                    "\u2063\u2063🆔: 1\n\n"
                    "🛒💵: Toshkent 170$\n"
                    "Keyboard gift\n"
                    "A33 8/256\n"
                    "rasxod\n\n"
                    "📞:+998901234567\n\n"
                    "Наличка\n💵:\n🇺🇿:\n\n"
                    "Card/Terminal/Paynet\n💵:\n🇺🇿:"
                )
            }

            async def forward_message(**kwargs):
                message_id = int(kwargs["message_id"])
                return SimpleNamespace(
                    message_id=900 + message_id,
                    chat_id=CHAT_ID,
                    caption=cards[message_id],
                    caption_entities=(),
                    text=None,
                    reply_markup=None,
                )

            async def edit_caption(**kwargs):
                cards[int(kwargs["message_id"])] = kwargs["caption"]
                return True

            bot = SimpleNamespace(
                forward_message=AsyncMock(side_effect=forward_message),
                edit_message_caption=AsyncMock(side_effect=edit_caption),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root, delivery_path), repo)

            await service.auto_correct_recent_cards(
                bot,
                reason="multiline-delivery-regression",
                reference_date=date(2026, 9, 1),
            )

            links = repo.delivery_links_for_orders(CHAT_ID, {124})
            self.assertEqual(len(links), 1)
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Otabek")
            self.assertIn("Доставка: Muzrob Oka", cards[200])
            self.assertIn("Статус: ✅ Доставлено (18:42)", cards[200])
            self.assertEqual(cards[200].count("Доставка:"), 1)

            await service.auto_correct_recent_cards(
                bot,
                reason="multiline-delivery-idempotency",
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(cards[200].count("Доставка:"), 1)

    async def test_malformed_card_preserves_existing_delivery_and_cached_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            create_delivery_db(delivery_path)
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            repo.sync_sale_details(
                CHAT_ID,
                200,
                ("+998 90 123 45 67",),
                "A33",
            )
            repo.upsert_delivery_link(
                CHAT_ID,
                10,
                delivery_order_id=1,
                delivery_order_number=11,
                matched_phone="+998 90 123 45 67",
                sale_date=date(2026, 9, 1),
            )
            body = (
                CARD.replace(
                    "rasxod:170$\n\n📞:",
                    "rasxod:170$\nmanager is still typing\n📞:",
                )
                + "\n\nДоставка: Muzrob Oka\nСтатус: 🚗 Курьер едет"
            )
            service = SalesPhotoService(settings(root, delivery_path), repo)

            normalized, manager_changed = service._apply_delivery_match(
                chat_id=CHAT_ID,
                source_message_id=10,
                body=body,
                entities=(),
                sale_date=date(2026, 9, 1),
                delivery_index=service._delivery_index(
                    date(2026, 9, 1),
                    date(2026, 9, 1),
                ),
                max_length=1024,
            )
            service._sync_sale_details(CHAT_ID, 200, body)

            self.assertFalse(normalized.changed)
            self.assertFalse(manager_changed)
            self.assertEqual(normalized.body, body)
            self.assertEqual(len(repo.delivery_links_for_orders(CHAT_ID, {1})), 1)
            cached = repo.call_sync_candidates(
                CHAT_ID,
                date(2026, 9, 1),
                date(2026, 9, 1),
            )[0]
            self.assertEqual(cached.phones, ("+998 90 123 45 67",))

    async def test_one_delivery_links_multiple_cards_and_ambiguity_unlinks_all(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            create_delivery_db(delivery_path)
            with sqlite3.connect(delivery_path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Abbos",
                        "Muzrob Oka",
                        None,
                        "on_way",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T08:30:00+00:00",
                        None,
                    ),
                )
                db.commit()
            repo = SalesPhotoRepository(root / "sales.db")
            cards = {}
            for offset in range(2):
                source_id = 10 + offset
                replacement_id = 200 + offset
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=date(2026, 9, 1),
                )
                repo.mark_reposted(CHAT_ID, source_id, replacement_id)
                repo.mark_complete(CHAT_ID, source_id)
                cards[replacement_id] = CARD.replace("🆔: 1", f"🆔: {offset + 1}")

            async def forward_message(**kwargs):
                message_id = int(kwargs["message_id"])
                return SimpleNamespace(
                    message_id=900 + message_id,
                    chat_id=CHAT_ID,
                    caption=cards[message_id],
                    caption_entities=(),
                    text=None,
                    reply_markup=None,
                )

            async def edit_caption(**kwargs):
                cards[int(kwargs["message_id"])] = kwargs["caption"]
                return True

            bot = SimpleNamespace(
                forward_message=AsyncMock(side_effect=forward_message),
                edit_message_caption=AsyncMock(side_effect=edit_caption),
                edit_message_text=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root, delivery_path), repo)

            await service.auto_correct_recent_cards(
                bot,
                reason="delivery-test",
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(len(repo.delivery_links_for_orders(CHAT_ID, {1})), 2)
            for replacement_id in (200, 201):
                self.assertIn("Доставка: Muzrob Oka", cards[replacement_id])
                self.assertIn("Статус: 🚗 Курьер едет", cards[replacement_id])
                self.assertEqual(repo.selected_manager(CHAT_ID, replacement_id), "Abbos")

            with sqlite3.connect(delivery_path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(2,12,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Ali",
                        "Courier 2",
                        None,
                        "pending",
                        "2026-09-01T09:00:00+00:00",
                        "2026-09-01T09:00:00+00:00",
                        None,
                    ),
                )
                db.commit()

            await service.auto_correct_recent_cards(
                bot,
                reason="delivery-ambiguous",
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(repo.delivery_links_for_orders(CHAT_ID, {1, 2}), ())
            for replacement_id in (200, 201):
                self.assertNotIn("Доставка:", cards[replacement_id])
                self.assertNotIn("Статус:", cards[replacement_id])
                self.assertIsNone(repo.selected_manager(CHAT_ID, replacement_id))

    async def test_linked_status_event_writes_actual_delivery_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            create_delivery_db(delivery_path)
            with sqlite3.connect(delivery_path) as db:
                db.execute(
                    "INSERT INTO orders VALUES(1,11,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "+998901234567",
                        None,
                        "Texnikach",
                        "Abbos",
                        "Muzrob Oka",
                        None,
                        "completed",
                        "2026-09-01T08:00:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                        "2026-09-01T13:42:00+00:00",
                    ),
                )
                db.commit()
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            repo.upsert_delivery_link(
                CHAT_ID,
                10,
                delivery_order_id=1,
                delivery_order_number=11,
                matched_phone="+998 90 123 45 67",
                sale_date=date(2026, 9, 1),
            )
            bot = SimpleNamespace(
                forward_message=AsyncMock(
                    return_value=SimpleNamespace(
                        message_id=900,
                        chat_id=CHAT_ID,
                        caption=CARD + "\n\nДоставка: Muzrob Oka\nСтатус: 🚗 Курьер едет",
                        caption_entities=(),
                        text=None,
                        reply_markup=None,
                    )
                ),
                edit_message_caption=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root, delivery_path), repo)

            await service._sync_linked_delivery_cards(bot, {1})

            caption = bot.edit_message_caption.await_args.kwargs["caption"]
            self.assertIn("Статус: ✅ Доставлено (18:42)", caption)
            self.assertNotIn("Будет", caption)


class DeliverySalesBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_request_publishes_once_and_links_exact_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_path = root / "delivery.db"
            delivery_repo = OrderRepository(delivery_path)
            delivery_repo.initialize()
            delivery = delivery_repo.create(
                manager_id=1,
                manager_name="Telegram Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901234567",
                    "product": "A57 Pro",
                },
            )
            delivery_repo.request_sales_card(
                delivery.id,
                actor_id=1,
                actor_name="Manager",
            )
            bridge = DeliverySalesBridge(delivery_path)
            pending = bridge.pending_sales_requests()
            self.assertEqual([item.id for item in pending], [delivery.id])
            claimed = bridge.claim_sales_request(delivery.id)
            self.assertIsNotNone(claimed)

            sales_repo = SalesPhotoRepository(root / "sales.db")
            service_settings = settings(root, delivery_path)
            service_settings = Settings(
                **{
                    **service_settings.__dict__,
                    "source_edit_grace_seconds": 0,
                    "startup_drain_seconds": 0,
                }
            )
            service = SalesPhotoService(service_settings, sales_repo)
            sent_ids = iter((100, 200))

            async def send_message(**kwargs):
                return SimpleNamespace(message_id=next(sent_ids))

            bot = SimpleNamespace(
                send_message=AsyncMock(side_effect=send_message),
                delete_message=AsyncMock(return_value=True),
            )
            service._sync_new_delivery_card = AsyncMock()
            service._sync_linked_delivery_cards = AsyncMock()

            source_id, replacement_id = await service._publish_delivery_sales_request(
                bot,
                claimed,
            )

            self.assertEqual((source_id, replacement_id), (100, 200))
            links = sales_repo.delivery_links_for_orders(CHAT_ID, {delivery.id})
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].delivery_order_number, delivery.order_number)
            self.assertEqual(sales_repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(
                bridge.finish_sales_request(
                    claimed,
                    source_message_id=100,
                    replacement_message_id=200,
                )
            )
            completed = delivery_repo.get(delivery.id)
            self.assertEqual(completed.sales_card_status, "complete")
            self.assertEqual(completed.sales_card_message_id, 200)

            # A retried/recovered request resolves the existing exact link
            # instead of publishing a second Telegram card.
            again = await service._publish_delivery_sales_request(bot, claimed)
            self.assertEqual(again, (100, 200))
            self.assertEqual(bot.send_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
