import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from app.database import OrderRepository
from app.handlers.orders import daily_delivery_log_action
from app.models import Order, OrderEvent
from app.utils.formatters import daily_delivery_report
from app.utils.static_map import (
    MAP_HEIGHT,
    MAP_WIDTH,
    TASHKENT_BOUNDS,
    WAREHOUSE_LATITUDE,
    WAREHOUSE_LONGITUDE,
    _viewport,
    _world_pixel,
    render_active_orders_map,
)


def _rgb_pixels(image):
    rgb_image = image.convert("RGB")
    get_flattened_data = getattr(rgb_image, "get_flattened_data", None)
    if get_flattened_data is not None:
        return get_flattened_data()
    return rgb_image.getdata()


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
                for red, green, blue in _rgb_pixels(image)
                if red > 180 and green < 90 and blue < 100
            )
        self.assertGreater(red_pixels, 100)

    async def test_map_always_shows_full_tashkent_and_warehouse(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.utils.static_map._load_tile",
                AsyncMock(return_value=Image.new("RGB", (256, 256), "#dbeafe")),
            ):
                result = await render_active_orders_map(
                    [],
                    cache_dir=Path(directory),
                )

        self.assertIsNotNone(result)
        zoom, center_x, center_y = _viewport([])
        self.assertEqual(zoom, 12)
        left = center_x - MAP_WIDTH / 2
        top = center_y - MAP_HEIGHT / 2
        for latitude, longitude in (*TASHKENT_BOUNDS, (WAREHOUSE_LATITUDE, WAREHOUSE_LONGITUDE)):
            world_x, world_y = _world_pixel(latitude, longitude, zoom)
            self.assertGreaterEqual(world_x - left, 0)
            self.assertLess(world_x - left, MAP_WIDTH)
            self.assertGreaterEqual(world_y - top, 0)
            self.assertLess(world_y - top, MAP_HEIGHT)

        with Image.open(result) as image:
            orange_pixels = sum(
                1
                for red, green, blue in _rgb_pixels(image)
                if red > 200 and 90 < green < 190 and blue < 80
            )
        self.assertGreater(orange_pixels, 100)

    def test_daily_report_is_one_chronological_timeline(self):
        order = Order(
            id=21,
            order_number=21,
            manager_id=11,
            manager_name="Abbos",
            seller_name="Ali",
            client_phone="+998901333999",
            product="Nokia A17",
            address_text="39B, Ахмада Дониша улица, Юнусабад, Ташкент",
            courier_id=1799690992,
            courier_name="Muzrob Oka",
            created_at="2026-08-23T04:00:00+00:00",
            time_started="2026-08-23T10:15:00+05:00",
            delivered_at="2026-08-23T11:05:00+05:00",
        )

        report = "\n".join(daily_delivery_report([order], date(2026, 8, 23)))

        arrived = report.index("09:00")
        departed = report.index("10:15")
        delivered = report.index("11:05")
        self.assertLess(arrived, departed)
        self.assertLess(departed, delivered)
        self.assertIn("Muzrob Oka выехал к заказу №21", report)
        self.assertIn("Muzrob Oka приехал и доставил заказ №21", report)
        self.assertIn("Ахмада Дониша", report)

    def test_daily_report_preserves_repeated_departure_and_undo_events(self):
        order = Order(
            id=21,
            order_number=21,
            manager_id=11,
            manager_name="Abbos",
            seller_name="Ali",
            client_phone="+998901333999",
            product="Nokia A17",
            address_text="Юнусабад, Ташкент",
        )
        statuses = [
            ("pending", "on_way", "2026-08-23T05:00:00+00:00"),
            ("on_way", "pending", "2026-08-23T05:10:00+00:00"),
            ("pending", "on_way", "2026-08-23T05:20:00+00:00"),
            ("on_way", "completed", "2026-08-23T06:00:00+00:00"),
        ]
        events = [
            OrderEvent(
                id=index,
                order_id=21,
                order_number=21,
                event_type="status_changed",
                actor_id=1799690992,
                actor_name="Muzrob Oka",
                actor_role="courier",
                from_status=from_status,
                to_status=to_status,
                created_at=created_at,
            )
            for index, (from_status, to_status, created_at) in enumerate(statuses, 1)
        ]

        report = "\n".join(
            daily_delivery_report([order], date(2026, 8, 23), events)
        )

        self.assertEqual(report.count("выехал к заказу №21"), 2)
        self.assertIn("10:10</b> · Muzrob Oka отменил выезд", report)
        self.assertIn("11:00</b> · Muzrob Oka приехал и доставил", report)

    def test_daily_report_ignores_same_status_edits(self):
        order = Order(
            id=21,
            order_number=21,
            manager_id=11,
            manager_name="Abbos",
            seller_name="Ali",
            client_phone="+998901333999",
            product="Nokia A17",
            status="completed",
        )
        event = OrderEvent(
            id=1,
            order_id=21,
            order_number=21,
            event_type="order_updated",
            from_status="completed",
            to_status="completed",
            changed_fields=("comment",),
            created_at="2026-08-23T06:00:00+00:00",
        )

        report = "\n".join(
            daily_delivery_report([order], date(2026, 8, 23), [event])
        )

        self.assertNotIn("доставил заказ", report)
        self.assertIn("За этот день действий пока нет", report)


class LogChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_log_chronology_button_is_disabled_without_new_posts(self):
        order = Order(
            id=21,
            order_number=21,
            manager_id=11,
            manager_name="Abbos",
            seller_name="Ali",
            client_phone="+998901333999",
            product="Nokia A17",
            courier_id=1799690992,
            courier_name="Muzrob Oka",
            address_text="Юнусабад, Ташкент",
            created_at="2026-08-23T04:00:00+00:00",
            time_started="2026-08-23T10:15:00+05:00",
            delivered_at="2026-08-23T11:05:00+05:00",
        )
        query = SimpleNamespace(
            data="daily_log:2026-08-23",
            from_user=SimpleNamespace(id=11),
            message=SimpleNamespace(chat_id=-1004459657817),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            bot=bot,
            application=SimpleNamespace(bot_data={
                "repo": SimpleNamespace(
                    list_all=Mock(return_value=[order]),
                    list_events_between=Mock(return_value=[]),
                ),
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    courier_ids=frozenset(),
                    orders_channel_id=-1004459657817,
                ),
            }),
        )

        await daily_delivery_log_action(
            SimpleNamespace(callback_query=query),
            context,
        )

        query.answer.assert_awaited_once_with(
            "Хронология в Log отключена",
            show_alert=True,
        )
        bot.send_message.assert_not_awaited()
