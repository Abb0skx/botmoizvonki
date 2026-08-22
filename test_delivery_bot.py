import tempfile
import unittest
from pathlib import Path

from app.database import OrderRepository
from app.utils.formatters import courier_card, manager_card
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

    def test_short_map_link_is_preserved(self):
        lat, lon, url = parse_location_url("https://yandex.com/maps/-/short")
        self.assertEqual((lat, lon), (None, None))
        self.assertEqual(url, "https://yandex.com/maps/-/short")


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

    def test_cards_escape_employee_text(self):
        order = self.repo.create(manager_id=1, manager_name="<Manager>", data={**self.data, "product": "A7 <Pro>"})
        self.assertIn("A7 &lt;Pro&gt;", manager_card(order))
        self.assertIn("&lt;Manager&gt;", courier_card(order))


if __name__ == "__main__":
    unittest.main()
