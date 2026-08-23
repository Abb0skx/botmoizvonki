from io import BytesIO
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from PIL import Image

from app.database import OrderRepository
from app.handlers.orders import _send_courier_map_log
from app.models import Order
from app.utils.static_map import MAP_HEIGHT, MAP_WIDTH, render_active_orders_map


class StaticMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_png_map_contains_active_order_markers(self):
        order = Order(
            id=1,
            order_number=125,
            manager_id=11,
            manager_name="Manager",
            client_phone="+998901333999",
            product="A57 Pro",
            latitude=41.338586,
            longitude=69.272757,
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.utils.static_map._load_tile",
                AsyncMock(return_value=Image.new("RGB", (256, 256), "#dbeafe")),
            ):
                result = await render_active_orders_map(
                    [order],
                    cache_dir=Path(directory),
                )

        self.assertIsNotNone(result)
        with Image.open(result) as image:
            self.assertEqual(image.size, (MAP_WIDTH, MAP_HEIGHT))
            self.assertEqual(image.format, "PNG")
            red_pixels = sum(
                1
                for red, green, blue in image.convert("RGB").get_flattened_data()
                if red > 180 and green < 90 and blue < 100
            )
        self.assertGreater(red_pixels, 100)


class CourierMapLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_log_contains_daily_and_active_counts_with_map(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "delivery.db"
            repo = OrderRepository(database_path)
            repo.initialize()
            completed = repo.create(
                manager_id=11,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "Delivered",
                    "amount_usd": 100,
                },
            )
            completed = repo.update(
                completed.id,
                status="completed",
                courier_id=1799690992,
                courier_name="Muzrob Oka",
                delivered_at=datetime.now(ZoneInfo("Asia/Tashkent")).isoformat(),
            )
            first_active = repo.create(
                manager_id=11,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "A57",
                    "amount_usd": 375,
                    "latitude": 41.338586,
                    "longitude": 69.272757,
                },
            )
            repo.update(
                first_active.id,
                status="pending",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
            )
            second_active = repo.create(
                manager_id=11,
                manager_name="Manager",
                data={
                    "seller_name": "Abbos",
                    "client_phone": "+998998904713",
                    "product": "Text address",
                    "amount_usd": 200,
                    "address_text": "Рынок Малика",
                },
            )
            repo.update(
                second_active.id,
                status="pending",
                assigned_courier_id=202134293,
                assigned_courier_name="Abbos",
            )
            image = BytesIO(b"fake-png")
            image.name = "active-deliveries.png"
            bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
            application = SimpleNamespace(
                bot=bot,
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        orders_channel_id=-1004459657817,
                        database_path=database_path,
                    ),
                },
            )

            with patch(
                "app.handlers.orders.render_active_orders_map",
                AsyncMock(return_value=image),
            ):
                await _send_courier_map_log(
                    application,
                    completed_order_id=completed.id,
                    courier_id=1799690992,
                    courier_name="Muzrob Oka",
                )

        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        caption = bot.send_photo.await_args.kwargs["caption"]
        self.assertIn("Доставлено сегодня: <b>1</b>", caption)
        self.assertIn("Осталось у курьера: <b>1</b>", caption)
        self.assertIn("Всего активных заказов: <b>2</b>", caption)
        self.assertIn("На карте: <b>1</b>", caption)

    async def test_ambiguous_photo_timeout_is_not_retried_as_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "delivery.db"
            repo = OrderRepository(database_path)
            repo.initialize()
            completed = repo.create(
                manager_id=11,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "Delivered",
                    "amount_usd": 100,
                },
            )
            completed = repo.update(
                completed.id,
                status="completed",
                courier_id=1799690992,
                courier_name="Muzrob Oka",
                delivered_at=datetime.now(ZoneInfo("Asia/Tashkent")).isoformat(),
            )
            active = repo.create(
                manager_id=11,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "Active",
                    "amount_usd": 120,
                    "latitude": 41.338586,
                    "longitude": 69.272757,
                },
            )
            repo.update(active.id, status="pending")
            image = BytesIO(b"fake-png")
            image.name = "active-deliveries.png"
            bot = SimpleNamespace(
                send_photo=AsyncMock(side_effect=RuntimeError("ambiguous timeout")),
                send_message=AsyncMock(),
            )
            application = SimpleNamespace(
                bot=bot,
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        orders_channel_id=-1004459657817,
                        database_path=database_path,
                    ),
                },
            )

            with (
                patch(
                    "app.handlers.orders.render_active_orders_map",
                    AsyncMock(return_value=image),
                ),
                self.assertLogs("app.handlers.orders", level="ERROR"),
            ):
                await _send_courier_map_log(
                    application,
                    completed_order_id=completed.id,
                    courier_id=1799690992,
                    courier_name="Muzrob Oka",
                )

        bot.send_photo.assert_awaited_once()
