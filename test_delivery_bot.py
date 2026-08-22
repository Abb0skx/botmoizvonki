import tempfile
import unittest
import sqlite3
from pathlib import Path

from app.database import OrderRepository
from app.database.repository import SCHEMA
from app.utils.formatters import all_locations_card, courier_card, manager_card, yandex_map_url, yandex_route_url
from app.utils.geocoding import extract_address, resolve_map_url
from app.utils.parsers import normalize_phone, parse_amount, parse_location_url


class ParserTests(unittest.TestCase):
    def test_phone_normalization(self):
        for raw in ("901333999", "90 133 39 99", "+998901333999", "998901333999"):
            self.assertEqual(normalize_phone(raw), "+998901333999")

    def test_invalid_phone(self):
        for raw in ("", "123", "+79991234567", "9989013339990"):
            with self.assertRaises(ValueError):
                normalize_phone(raw)

    def test_amount_parsing(self):
        cases = {
            "100": (100, None),
            "100$": (100, None),
            "100$ 1920000": (100, 1920000),
            "100 1920000": (100, 1920000),
            "1 920 000 сум": (None, 1920000),
            "1920000": (None, 1920000),
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_amount(raw), expected)

    def test_invalid_amount(self):
        for raw in ("", "нет суммы", "0", "-100"):
            with self.assertRaises(ValueError):
                parse_amount(raw)

    def test_yandex_coordinates(self):
        lat, lon, _ = parse_location_url("https://yandex.uz/maps/?ll=69.240562%2C41.311081")
        self.assertEqual((lat, lon), (41.311081, 69.240562))

    def test_google_and_yandex_route_coordinates(self):
        self.assertEqual(
            parse_location_url("https://maps.google.com/?q=41.311081,69.240562")[:2],
            (41.311081, 69.240562),
        )
        self.assertEqual(
            parse_location_url("https://yandex.uz/maps/?rtext=~41.311081%2C69.240562&rtt=auto")[:2],
            (41.311081, 69.240562),
        )

    def test_short_map_link_is_preserved(self):
        lat, lon, url = parse_location_url("https://yandex.com/maps/-/short")
        self.assertEqual((lat, lon), (None, None))
        self.assertEqual(url, "https://yandex.com/maps/-/short")

    def test_extract_district_and_mahalla(self):
        result = extract_address({
            "display_name": "Дом 1, Махалля Бунёдкор, Чиланзарский район, Ташкент",
            "address": {"city_district": "Чиланзарский район", "neighbourhood": "Махалля Бунёдкор"},
        })
        self.assertEqual(result["district"], "Чиланзарский район")
        self.assertEqual(result["mahalla"], "Махалля Бунёдкор")


class MapUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_parsed_yandex_url_without_network(self):
        result = await resolve_map_url("https://yandex.uz/maps/?ll=69.240562%2C41.311081")
        self.assertEqual(result[:2], (41.311081, 69.240562))

    async def test_reject_non_map_domain(self):
        with self.assertRaises(ValueError):
            await resolve_map_url("https://example.com/?q=41.3,69.2")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        self.data = {"client_phone": "+998901333999", "product": "A7 Pro", "amount_usd": 100}

    def tearDown(self):
        self.tempdir.cleanup()

    def test_order_numbers_are_incremental(self):
        self.assertEqual(self.repo.create(manager_id=1, manager_name="A", data=self.data).order_number, 1)
        self.assertEqual(self.repo.create(manager_id=1, manager_name="A", data=self.data).order_number, 2)

    def test_existing_database_gets_address_migration(self):
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        legacy_schema = SCHEMA
        for column in ("address_text", "district", "mahalla"):
            legacy_schema = legacy_schema.replace(f"    {column} TEXT,\n", "")
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
        migrated = OrderRepository(legacy_path)
        migrated.initialize()
        with migrated.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        self.assertTrue({"address_text", "district", "mahalla"}.issubset(columns))

    def test_atomic_courier_claim(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="pending")
        claimed = self.repo.transition(
            order.id, {"pending"}, status="on_way", courier_id=10, courier_name="Courier 1",
            guard_courier_id=10, require_unassigned_or_same=True,
        )
        rejected = self.repo.transition(
            order.id, {"pending"}, status="on_way", courier_id=11, courier_name="Courier 2",
            guard_courier_id=11, require_unassigned_or_same=True,
        )
        self.assertEqual(claimed.courier_id, 10)
        self.assertIsNone(rejected)

    def test_delivery_state_survives_repository_recreation(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="awaiting_photo", courier_id=10, courier_name="Courier")
        reopened = OrderRepository(self.repo.path)
        self.assertEqual(reopened.get_active_delivery(10).id, order.id)

    def test_address_columns_are_saved(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.311081,
                "longitude": 69.240562,
                "address_text": "Ташкент",
                "district": "Чиланзарский район",
                "mahalla": "Бунёдкор",
            },
        )
        self.assertEqual(order.district, "Чиланзарский район")
        self.assertEqual(order.mahalla, "Бунёдкор")

    def test_undo_on_way_returns_order_to_queue(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="on_way", courier_id=10, courier_name="Courier", time_started="now")
        returned = self.repo.transition(
            order.id,
            {"on_way"},
            status="pending",
            courier_id=None,
            courier_name=None,
            time_started=None,
            guard_courier_id=10,
            require_unassigned_or_same=True,
        )
        self.assertEqual(returned.status, "pending")
        self.assertIsNone(returned.courier_id)
        self.assertIsNone(returned.time_started)

    def test_active_locations_map_contains_models(self):
        first = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "latitude": 41.31, "longitude": 69.24, "mahalla": "Бунёдкор"},
        )
        second = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "product": "S25 Ultra", "latitude": 41.32, "longitude": 69.25},
        )
        self.repo.update(first.id, status="pending")
        self.repo.update(second.id, status="on_way", courier_id=10)
        text, map_url = all_locations_card(self.repo.list_active())
        self.assertIn("A7 Pro", text)
        self.assertIn("S25 Ultra", text)
        self.assertIn("Бунёдкор", text)
        self.assertTrue(map_url.startswith("https://yandex.uz/maps/?"))
        self.assertIn("rtext", yandex_route_url(self.repo.get(first.id)))
        self.assertIn("pt", yandex_map_url(self.repo.get(first.id)))

    def test_cards_escape_employee_text(self):
        order = self.repo.create(manager_id=1, manager_name="<Manager>", data={**self.data, "product": "A7 <Pro>"})
        self.assertIn("A7 &lt;Pro&gt;", manager_card(order))
        self.assertIn("&lt;Manager&gt;", courier_card(order))


if __name__ == "__main__":
    unittest.main()
