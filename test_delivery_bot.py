import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.bot.keyboards import (
    all_locations_keyboard, completed_keyboard, courier_cancelled_keyboard, courier_keyboard,
    delivery_pending_keyboard, location_channel_keyboard, manager_cancelled_keyboard,
    manager_sent_keyboard, review_keyboard,
)
from app.database import OrderRepository
from app.database.repository import MIGRATION_COLUMNS, SCHEMA
from app.handlers.orders import (
    DETAILS, PAYMENT, SECOND_LOCATION, _courier_waiting_pickup_text,
    _location_values, _message_location_urls, _publish_location,
    _waiting_pickup_reminder_messages, courier_action, delivery_input, details,
    location_label_action, product, save_edit, second_location,
)
from app.utils.formatters import (
    all_locations_card, completed_card, courier_card, manager_card, short_address,
    telegram_location_url, telegram_message_url, yandex_map_url,
    yandex_route_url,
)
from app.utils.geocoding import (
    _cache_map_resolution, _map_url_cache, extract_address, extract_text_address,
    resolve_map_url,
)
from app.utils.parsers import normalize_phone, parse_amount, parse_location_url, parse_order_details
from app.utils.payments import PAID_AT_ASSEMBLY, normalize_payment
from app.utils.sellers import normalize_seller


class _FakeResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeMapClient:
    """Exercise resolver redirect logic with either get() or stream()."""

    def __init__(self, response_for):
        self.response_for = response_for
        self.requested_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, *args, **kwargs):
        self.requested_urls.append(str(url))
        return self.response_for(str(url))

    def stream(self, method, url, *args, **kwargs):
        self.requested_urls.append(str(url))
        return _FakeResponseContext(self.response_for(str(url)))


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
            "9000": (9000, None),
            "9001": (None, 9001),
            "7️⃣\nA56\n375$": (375, None),
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_amount(raw), expected)

    def test_invalid_amount(self):
        for raw in ("", "нет суммы", "0", "-100"):
            with self.assertRaises(ValueError):
                parse_amount(raw)

    def test_combined_order_details(self):
        result = parse_order_details(
            "Телефон: 90 133 39 99\n"
            "Цена: 100$ 1 920 000\n"
            "Локация: https://yandex.uz/maps/?ll=69.240562%2C41.311081"
        )
        self.assertEqual(result["client_phone"], "+998901333999")
        self.assertEqual((result["amount_usd"], result["amount_uzs"]), (100, 1920000))
        self.assertEqual(
            result["location_url"],
            "https://yandex.uz/maps/?ll=69.240562%2C41.311081",
        )

    def test_order_details_can_arrive_separately(self):
        self.assertEqual(parse_order_details("901333999")["client_phone"], "+998901333999")
        self.assertEqual(parse_order_details("100$ 1920000")["amount_usd"], 100)
        self.assertEqual(
            parse_order_details("https://yandex.uz/maps/?ll=69.24%2C41.31")["location_url"],
            "https://yandex.uz/maps/?ll=69.24%2C41.31",
        )

    def test_large_uzs_amount_is_not_mistaken_for_phone(self):
        result = parse_order_details("100000000 сум")
        self.assertNotIn("client_phone", result)
        self.assertEqual(result["amount_uzs"], 100000000)

    def test_seller_normalization(self):
        self.assertEqual(normalize_seller("otabek"), "Otabek")
        with self.assertRaises(ValueError):
            normalize_seller("Другой")

    def test_payment_normalization(self):
        self.assertEqual(normalize_payment("✅ Оплачено при сборе товара"), PAID_AT_ASSEMBLY)
        with self.assertRaises(ValueError):
            normalize_payment("потом")

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

    def test_google_official_query_and_destination_urls(self):
        cases = (
            (
                "https://www.google.com/maps/search/?api=1&query=41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
            (
                "https://www.google.com/maps/dir/?api=1&origin=41.338586%2C69.272757"
                "&destination=41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
            (
                "https://maps.google.com/maps?daddr=41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
            (
                "https://www.google.com/maps/@?api=1&center=41.300000%2C69.200000"
                "&query=41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_location_url(url)[:2], expected)

    def test_google_exact_place_marker_wins_over_map_view_center(self):
        url = (
            "https://www.google.com/maps/place/TEXNIKACH/"
            "@41.300000,69.200000,14z/data=!4m6!3m5!8m2!3d41.311081!4d69.240562"
        )

        self.assertEqual(parse_location_url(url)[:2], (41.311081, 69.240562))

    def test_google_browser_directions_uses_the_last_exact_point(self):
        url = (
            "https://www.google.com/maps/dir/Warehouse/Client/"
            "@41.300000,69.200000/data=!3d41.338586!4d69.272757"
            "!3d41.311081!4d69.240562"
        )

        self.assertEqual(parse_location_url(url)[:2], (41.311081, 69.240562))

    def test_yandex_whatshere_marker_wins_over_map_view_center(self):
        url = (
            "https://yandex.uz/maps/?ll=69.200000%2C41.300000"
            "&whatshere%5Bpoint%5D=69.240562%2C41.311081&z=13"
        )

        self.assertEqual(parse_location_url(url)[:2], (41.311081, 69.240562))

    def test_apple_waze_openstreetmap_and_2gis_coordinates(self):
        cases = (
            (
                "https://maps.apple.com/?ll=41.311081%2C69.240562&q=Client+41.1%2C69.1",
                (41.311081, 69.240562),
            ),
            (
                "https://maps.apple.com/?q=41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
            (
                "https://www.waze.com/ul?ll=41.311081%2C69.240562&navigate=yes",
                (41.311081, 69.240562),
            ),
            (
                "https://www.openstreetmap.org/?mlat=41.311081&mlon=69.240562"
                "#map=13/41.300000/69.200000",
                (41.311081, 69.240562),
            ),
            (
                "https://www.openstreetmap.org/directions?"
                "route=41.338586%2C69.272757%3B41.311081%2C69.240562",
                (41.311081, 69.240562),
            ),
            (
                "https://2gis.uz/tashkent?m=69.240562%2C41.311081%2F18",
                (41.311081, 69.240562),
            ),
            (
                "https://2gis.uz/tashkent/firm/700000010/69.240562,41.311081"
                "?m=69.200000%2C41.300000%2F12",
                (41.311081, 69.240562),
            ),
            (
                "https://2gis.ru/museum?return_url=https%3A%2F%2F2gis.ru%2Ftashkent"
                "%3FqueryState%3Dcenter%252F69.240562%252C41.311081%252Fzoom%252F16",
                (41.311081, 69.240562),
            ),
            (
                "https://2gis.uz/directions/points/69.200000,41.300000%7C"
                "69.240562,41.311081",
                (41.311081, 69.240562),
            ),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_location_url(url)[:2], expected)

    def test_map_url_is_extracted_from_wrapper_text(self):
        url = "https://maps.apple.com/?ll=41.311081%2C69.240562"
        parsed = parse_order_details(f"Вот локация клиента: {url} (второй подъезд).")

        self.assertEqual(parsed["location_urls"], [url])

    def test_hidden_telegram_text_link_is_extracted(self):
        url = "https://maps.app.goo.gl/AbCdEf123"
        entity = SimpleNamespace(type="text_link", url=url, offset=0, length=8)
        message = SimpleNamespace(
            text="Локация",
            caption=None,
            entities=(entity,),
            caption_entities=(),
        )

        self.assertEqual(_message_location_urls(message), [url])

    def test_unrelated_link_does_not_hide_two_following_map_links(self):
        google = "https://maps.google.com/?q=41.311081,69.240562"
        yandex = "https://yandex.uz/maps/?ll=69.250000%2C41.320000"
        message = SimpleNamespace(
            text=f"https://texnikach.uz/catalog\n{google}\n{yandex}",
            caption=None,
            entities=(),
            caption_entities=(),
        )

        self.assertEqual(_message_location_urls(message), [google, yandex])

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

    def test_text_address_extracts_canonical_district_and_mahalla(self):
        result = extract_text_address(
            "Yashnobod tumani, mahalla Alimkent, Кустанай улица"
        )

        self.assertEqual(result["district"], "Яшнабадский район")
        self.assertEqual(result["mahalla"], "Alimkent")
        self.assertIsNone(extract_text_address("Ориентир возле школы")["district"])

    def test_private_channel_message_link(self):
        self.assertEqual(
            telegram_message_url(-1004398605075, 125),
            "https://t.me/c/4398605075/125",
        )
        self.assertIsNone(telegram_message_url(-5125237049, 125))
        self.assertIsNone(telegram_message_url(-1004398605075, None))


class MapUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_map_resolution_cache_stays_bounded(self):
        previous = dict(_map_url_cache)
        _map_url_cache.clear()
        try:
            for index in range(257):
                _cache_map_resolution(
                    f"https://maps.app.goo.gl/cache-{index}",
                    41.3,
                    69.2,
                    f"https://www.google.com/maps/?q=41.3,69.2&item={index}",
                    3600,
                )
            self.assertEqual(len(_map_url_cache), 256)
            self.assertNotIn("https://maps.app.goo.gl/cache-0", _map_url_cache)
            self.assertIn("https://maps.app.goo.gl/cache-256", _map_url_cache)
        finally:
            _map_url_cache.clear()
            _map_url_cache.update(previous)

    async def test_resolve_parsed_yandex_url_without_network(self):
        result = await resolve_map_url("https://yandex.uz/maps/?ll=69.240562%2C41.311081")
        self.assertEqual(result[:2], (41.311081, 69.240562))

    async def test_reject_non_map_domain(self):
        with self.assertRaises(ValueError):
            await resolve_map_url("https://example.com/?q=41.3,69.2")

    async def test_resolve_allowed_google_short_redirect(self):
        short_url = "https://maps.app.goo.gl/AbCdEf123"
        resolved_url = (
            "https://www.google.com/maps/search/?api=1"
            "&query=41.311081%2C69.240562"
        )

        def response_for(url):
            if url == short_url:
                return httpx.Response(
                    302,
                    headers={"location": resolved_url},
                    request=httpx.Request("GET", short_url),
                )
            return httpx.Response(
                200,
                request=httpx.Request("GET", resolved_url),
            )

        client = _FakeMapClient(response_for)

        with patch("app.utils.geocoding.httpx.AsyncClient", return_value=client):
            result = await resolve_map_url(short_url)

        self.assertEqual(result[:2], (41.311081, 69.240562))
        self.assertEqual(result[2], resolved_url)

    async def test_resolve_short_link_from_bounded_html_map_metadata(self):
        short_url = "https://maps.app.goo.gl/HtmlMetadata123"
        resolved_url = (
            "https://www.google.com/maps/search/?api=1"
            "&query=41.311081%2C69.240562"
        )
        html = (
            '<html><head><meta property="og:url" content="'
            + resolved_url.replace("&", "&amp;")
            + '"></head></html>'
        )
        client = _FakeMapClient(
            lambda url: httpx.Response(
                200,
                content=html.encode(),
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("GET", url),
            )
        )

        with patch("app.utils.geocoding.httpx.AsyncClient", return_value=client):
            result = await resolve_map_url(short_url)

        self.assertEqual(result[:2], (41.311081, 69.240562))
        self.assertEqual(result[2], resolved_url)

    async def test_short_redirect_to_unapproved_host_fails_closed(self):
        short_url = "https://maps.app.goo.gl/UnsafeRedirect123"

        def response_for(url):
            return httpx.Response(
                302,
                headers={
                    "location": "https://maps.attacker.example/?q=41.311081,69.240562",
                },
                request=httpx.Request("GET", url),
            )

        client = _FakeMapClient(response_for)

        with patch("app.utils.geocoding.httpx.AsyncClient", return_value=client):
            result = await resolve_map_url(short_url)

        self.assertEqual(result, (None, None, short_url))
        self.assertEqual(client.requested_urls, [short_url])

    async def test_embedded_static_map_viewport_is_not_used_as_customer_location(self):
        short_url = "https://maps.app.goo.gl/NoCoordinates123"
        html = (
            '<img src="https://maps.google.com/maps/api/staticmap?'
            'center=41.2975104%2C69.2617216&amp;zoom=12">'
        )
        client = _FakeMapClient(
            lambda url: httpx.Response(
                200,
                content=html.encode(),
                request=httpx.Request("GET", url),
            )
        )

        with patch("app.utils.geocoding.httpx.AsyncClient", return_value=client):
            result = await resolve_map_url(short_url)

        self.assertEqual(result, (None, None, short_url))

    async def test_yandex_object_page_uses_only_coordinates_tied_to_its_org_id(self):
        object_url = "https://yandex.uz/maps/org/texnikach/133297049157/"
        html = (
            '<div data-object="search-result" data-id="999" '
            'data-coordinates="69.100000,41.100000"></div>'
            '<div data-object="search-result" data-id="133297049157" '
            'data-coordinates="69.248501,41.316772"></div>'
        )
        client = _FakeMapClient(
            lambda url: httpx.Response(
                200,
                content=html.encode(),
                request=httpx.Request("GET", url),
            )
        )

        with patch("app.utils.geocoding.httpx.AsyncClient", return_value=client):
            result = await resolve_map_url(object_url)

        self.assertEqual(result[:2], (41.316772, 69.248501))


class PickupReminderMessageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def create_order(
        self,
        product: str,
        *,
        status: str = "pending",
        courier_id: int | None = 1799690992,
        courier_name: str | None = "Muzrob & Oka",
        seller_name: str = "Ali",
    ):
        order = self.repo.create(
            manager_id=101,
            manager_name="Manager",
            data={
                "seller_name": seller_name,
                "client_phone": "+998901333999",
                "product": product,
                "amount_usd": 100,
            },
        )
        return self.repo.update(
            order.id,
            status=status,
            assigned_courier_id=courier_id,
            assigned_courier_name=courier_name,
        )

    def test_reminder_is_empty_without_assigned_pending_orders(self):
        self.create_order(
            "Pending without courier",
            courier_id=None,
            courier_name=None,
        )
        self.create_order("Already picked up", status="picked_up")
        self.create_order("Already on way", status="on_way")

        self.assertEqual(_waiting_pickup_reminder_messages(self.repo), [])

    def test_reminder_filters_and_groups_pending_orders_by_courier(self):
        muzrob_orders = [
            self.create_order("A57 Pro"),
            self.create_order("A56"),
        ]
        olmas_orders = [
            self.create_order(
                "iPhone 16",
                courier_id=7636344727,
                courier_name="Olmas <Lead>",
                seller_name="Abbos",
            ),
            self.create_order(
                "Samsung S25",
                courier_id=7636344727,
                courier_name="Olmas <Lead>",
                seller_name="Otabek",
            ),
        ]
        excluded = [
            self.create_order(
                "Unassigned",
                courier_id=None,
                courier_name=None,
            ),
            self.create_order("Picked up", status="picked_up"),
            self.create_order("On way", status="on_way"),
            self.create_order("Completed", status="completed"),
            self.create_order("Cancelled", status="cancelled"),
        ]

        messages = _waiting_pickup_reminder_messages(self.repo)
        combined = "\n".join(messages)

        self.assertEqual(len(messages), 1)
        for order in [*muzrob_orders, *olmas_orders]:
            self.assertEqual(combined.count(f"Заказ №{order.order_number} ·"), 1)
        for order in excluded:
            self.assertNotIn(f"Заказ №{order.order_number} ·", combined)

        # A courier name is a group heading, not repeated on every order row.
        self.assertEqual(combined.count("Muzrob &amp; Oka"), 1)
        self.assertEqual(combined.count("Olmas &lt;Lead&gt;"), 1)
        lines = combined.splitlines()
        self.assertTrue(
            any("Muzrob &amp; Oka" in line and "<b>" in line for line in lines)
        )
        self.assertTrue(
            any("Olmas &lt;Lead&gt;" in line and "<b>" in line for line in lines)
        )
        self.assertIn("забрали", combined.casefold())

    def test_reminder_paginates_losslessly_and_escapes_html(self):
        orders = [
            self.create_order(
                f"MODEL-{index:02d} " + "<&>" * 44,
                courier_name="Muzrob <Courier & Co>",
                seller_name="Ali <Manager & Co>",
            )
            for index in range(24)
        ]

        messages = _waiting_pickup_reminder_messages(self.repo)
        combined = "\n".join(messages)

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 4096 for message in messages))
        for order in orders:
            self.assertEqual(combined.count(f"Заказ №{order.order_number} ·"), 1)
        self.assertTrue(
            all("Muzrob &lt;Courier &amp; Co&gt;" in message for message in messages)
        )
        self.assertIn("Ali &lt;Manager &amp; Co&gt;", combined)
        self.assertIn("&lt;&amp;&gt;", combined)
        self.assertNotIn("<Courier", combined)
        self.assertNotIn("<Manager", combined)
        self.assertNotIn("<&>", combined)
        self.assertTrue(all("забрали" in message.casefold() for message in messages))


class HandlerFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context() -> SimpleNamespace:
        return SimpleNamespace(
            user_data={"draft": {"seller_name": "Ali", "product": "A7 Pro"}},
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({1})),
            }),
        )

    @staticmethod
    def update(text: str) -> SimpleNamespace:
        message = SimpleNamespace(text=text, location=None, reply_text=AsyncMock())
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=1, full_name="Manager", username=None),
        )

    async def test_edit_location_accepts_a_labelled_google_link(self):
        message = SimpleNamespace(
            text="Локация: https://maps.google.com/?q=41.311081,69.240562",
            caption=None,
            location=None,
            venue=None,
            entities=(),
            caption_entities=(),
        )
        address = {"address_text": None, "district": None, "mahalla": None}

        with patch("app.utils.geocoding.reverse_geocode", AsyncMock(return_value=address)):
            values = await _location_values(message)

        self.assertEqual((values["latitude"], values["longitude"]), (41.311081, 69.240562))

    async def test_venue_replaces_text_location_prompt(self):
        context = self.context()
        context.user_data["draft"]["awaiting_text_location"] = True
        update = self.update(None)
        update.message.venue = SimpleNamespace(
            location=SimpleNamespace(latitude=41.311081, longitude=69.240562)
        )
        update.message.caption = None
        update.message.entities = ()
        update.message.caption_entities = ()
        address = {"address_text": None, "district": None, "mahalla": None}

        with patch("app.utils.geocoding.reverse_geocode", AsyncMock(return_value=address)):
            state = await details(update, context)

        self.assertEqual(state, DETAILS)
        self.assertEqual(
            (context.user_data["draft"]["latitude"], context.user_data["draft"]["longitude"]),
            (41.311081, 69.240562),
        )
        self.assertNotIn("awaiting_text_location", context.user_data["draft"])

    async def test_product_step_uses_short_details_prompt(self):
        update = self.update("A57 Pro")

        state = await product(update, self.context())

        self.assertEqual(state, DETAILS)
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "3/6. Отправьте:\n📍 Локацию\n📱 Номер\n💰 Общую сумму",
        )

    async def test_waiting_pickup_log_lists_only_pending_products_for_courier(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            first = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "A57 Pro",
                    "amount_usd": 100,
                },
            )
            repo.update(
                first.id,
                status="pending",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
            )
            second = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Abbos",
                    "client_phone": "+998901333999",
                    "product": "A56",
                    "amount_usd": 200,
                },
            )
            repo.update(
                second.id,
                status="pending",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
            )
            picked_up = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Olmas",
                    "client_phone": "+998901333999",
                    "product": "Не показывать",
                    "amount_usd": 300,
                },
            )
            repo.update(
                picked_up.id,
                status="picked_up",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
            )

            text = _courier_waiting_pickup_text(repo, 1799690992, "Muzrob Oka")

        self.assertEqual(
            text,
            "⏳ <b>Ждём курьера Muzrob Oka</b>\n"
            "1. Заказ №1 · Ali · A57 Pro\n"
            "2. Заказ №2 · Abbos · A56",
        )

    async def test_combined_details_complete_collection(self):
        update = self.update(
            "Телефон: 90 133 39 99\n"
            "Цена: 100$ 1920000\n"
            "Локация: https://yandex.uz/maps/?ll=69.24%2C41.31"
        )
        context = self.context()
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.24%2C41.31",
            "latitude": 41.31,
            "longitude": 69.24,
            "address_text": "Ташкент",
            "district": None,
            "mahalla": None,
        }
        with patch("app.handlers.orders.enrich_location", AsyncMock(return_value=location)):
            state = await details(update, context)
        self.assertEqual(state, PAYMENT)
        self.assertEqual(context.user_data["draft"]["client_phone"], "+998901333999")
        self.assertEqual(context.user_data["draft"]["amount_uzs"], 1920000)

    async def test_details_accumulate_in_any_order(self):
        context = self.context()
        price_update = self.update("100$ 1920000")
        self.assertEqual(await details(price_update, context), DETAILS)
        phone_update = self.update("901333999")
        self.assertEqual(await details(phone_update, context), DETAILS)

        map_update = self.update("https://yandex.uz/maps/?ll=69.24%2C41.31")
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.24%2C41.31",
            "latitude": 41.31,
            "longitude": 69.24,
            "address_text": None,
            "district": None,
            "mahalla": None,
        }
        with patch("app.handlers.orders.enrich_location", AsyncMock(return_value=location)):
            state = await details(map_update, context)
        self.assertEqual(state, PAYMENT)

    async def test_optional_second_location_is_saved(self):
        context = self.context()
        context.user_data["draft"].update({
            "client_phone": "+998901333999",
            "amount_usd": 100,
            "latitude": 41.31,
            "longitude": 69.24,
        })
        update = self.update("https://yandex.uz/maps/?ll=69.25%2C41.32")
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.25%2C41.32",
            "latitude": 41.32,
            "longitude": 69.25,
            "address_text": "Вторая точка, Ташкент",
            "district": "Чиланзарский район",
            "mahalla": "Бунёдкор",
        }
        with patch("app.handlers.orders._location_values", AsyncMock(return_value=location)):
            state = await second_location(update, context)
        self.assertEqual(state, PAYMENT)
        self.assertEqual(context.user_data["draft"]["second_latitude"], 41.32)
        self.assertEqual(context.user_data["draft"]["second_longitude"], 69.25)

    async def test_basic_group_location_button_is_silent(self):
        query = SimpleNamespace(answer=AsyncMock())

        await location_label_action(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(),
        )

        query.answer.assert_awaited_once_with()

    async def test_edit_amount_saves_and_confirms_new_price(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                },
            )
            message = SimpleNamespace(
                text="120$ 1 536 000",
                location=None,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=1, type="private"),
                effective_user=SimpleNamespace(id=1, full_name="Manager", username=None),
                message=message,
            )
            bot = SimpleNamespace(edit_message_text=AsyncMock())
            context = SimpleNamespace(
                user_data={
                    "edit": {
                        "order_id": order.id,
                        "field": "amount",
                        "message_id": 50,
                        "chat_id": 1,
                    }
                },
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(manager_ids=frozenset({1})),
                }),
                bot=bot,
            )

            await save_edit(update, context)

            updated = repo.get(order.id)
            self.assertEqual((updated.amount_usd, updated.amount_uzs), (120, 1536000))
            self.assertNotIn("edit", context.user_data)
            self.assertIn("Новая цена сохранена", message.reply_text.await_args.args[0])

    async def test_delivery_input_ignores_anonymous_channel_post(self):
        message = SimpleNamespace(
            photo=[],
            text="служебная запись канала",
            caption=None,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1004459657817),
            effective_user=None,
            message=message,
        )
        settings = SimpleNamespace(courier_ids=frozenset({2}))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        await delivery_input(update, context)

        message.reply_text.assert_not_awaited()

    async def test_delivery_completes_from_photo_with_price_caption(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "payment_status": PAID_AT_ASSEMBLY,
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                },
            )
            repo.update(
                order.id,
                status="awaiting_photo",
                courier_id=2,
                courier_name="Courier",
                delivery_chat_id=-100,
                delivery_message_id=50,
            )
            message = SimpleNamespace(
                photo=[SimpleNamespace(file_id="photo-file-id")],
                text=None,
                caption="100$ 1 920 000",
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=-100),
                effective_user=SimpleNamespace(id=2),
                message=message,
            )
            bot = SimpleNamespace(edit_message_text=AsyncMock(), send_photo=AsyncMock())
            settings = SimpleNamespace(
                delivery_group_id=-100,
                courier_ids=frozenset({2}),
                orders_channel_id=-1004459657817,
            )
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings, "repo": repo}),
                bot=bot,
            )

            await delivery_input(update, context)

            completed = repo.get(order.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.delivery_photo, "photo-file-id")
            self.assertIsNotNone(completed.delivered_at)
            self.assertEqual((completed.received_usd, completed.received_uzs), (100, 1920000))
            bot.send_photo.assert_awaited_once()
            self.assertEqual(bot.send_photo.await_args.kwargs["chat_id"], -1004459657817)
            self.assertIn("✅ Оплачено", bot.send_photo.await_args.kwargs["caption"])
            self.assertIn("100$", bot.send_photo.await_args.kwargs["caption"])
            message.reply_text.assert_awaited_once_with("✅ Доставка подтверждена.")

            collect_order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Olmas",
                    "client_phone": "+998901333999",
                    "product": "S25 Ultra",
                    "amount_usd": 150,
                },
            )
            repo.update(
                collect_order.id,
                status="awaiting_photo",
                courier_id=2,
                courier_name="Courier",
                delivery_chat_id=-100,
                delivery_message_id=51,
            )
            collect_message = SimpleNamespace(
                photo=[SimpleNamespace(file_id="second-photo")],
                text=None,
                caption="150$ 2 400 000",
                reply_text=AsyncMock(),
            )
            update.message = collect_message

            await delivery_input(update, context)

            completed_collect = repo.get(collect_order.id)
            self.assertEqual(completed_collect.status, "completed")
            self.assertEqual(
                (completed_collect.received_usd, completed_collect.received_uzs),
                (150, 2400000),
            )
            collect_message.reply_text.assert_awaited_once_with("✅ Доставка подтверждена.")
            self.assertEqual(bot.send_photo.await_count, 2)

    async def test_delivered_button_completes_and_sends_optional_photo_prompt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Abbos",
                    "client_phone": "+998998904713",
                    "product": "A56",
                    "amount_usd": 375,
                },
            )
            repo.update(
                order.id,
                status="on_way",
                assigned_courier_id=2,
                assigned_courier_name="Courier",
                courier_id=2,
                courier_name="Courier",
                picked_up_at="2026-08-24T10:00:00+05:00",
                time_started="2026-08-24T10:05:00+05:00",
                delivery_chat_id=-100,
                delivery_message_id=50,
            )
            query = SimpleNamespace(
                data=f"complete:{order.id}",
                from_user=SimpleNamespace(id=2, full_name="Courier", username=None),
                message=SimpleNamespace(chat_id=-100, message_id=50),
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query)
            bot = SimpleNamespace(send_message=AsyncMock())
            settings = SimpleNamespace(delivery_group_id=-100, courier_ids=frozenset({2}))
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings, "repo": repo}),
                bot=bot,
            )

            await courier_action(update, context)

            completed = repo.get(order.id)
            self.assertEqual(completed.status, "completed")
            self.assertIsNone(completed.delivery_photo)
            query.answer.assert_awaited_once_with("Заказ доставлен")
            self.assertIn("📦 A56", query.edit_message_text.await_args.args[0])
            self.assertEqual(
                query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data,
                f"undo_complete:{order.id}",
            )
            self.assertEqual(bot.send_message.await_count, 1)
            prompt = bot.send_message.await_args
            self.assertEqual(
                prompt.kwargs["text"],
                f"🚚 Заказ №{order.order_number} · A56\n"
                "Courier, отправьте фото и цену товара 📸💰",
            )

    async def test_publish_location_saves_channel_message(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                    "latitude": 41.311081,
                    "longitude": 69.240562,
                },
            )
            repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-5125237049,
                delivery_message_id=50,
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(side_effect=[
                    SimpleNamespace(chat_id=-1004398605075, message_id=87),
                    SimpleNamespace(chat_id=-1004398605075, message_id=89),
                    SimpleNamespace(chat_id=-1004398605075, message_id=90),
                    SimpleNamespace(chat_id=-1004398605075, message_id=92),
                ]),
                send_location=AsyncMock(
                    return_value=SimpleNamespace(chat_id=-1004398605075, message_id=88)
                ),
                delete_message=AsyncMock(),
            )
            settings = SimpleNamespace(location_channel_id=-1004398605075)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings}),
                bot=bot,
            )

            published = await _publish_location(context, repo, repo.get(order.id))

            bot.send_location.assert_awaited_once()
            self.assertEqual(published.location_chat_id, -1004398605075)
            self.assertEqual(published.location_message_id, 88)
            self.assertEqual(published.location_details_message_id, 87)
            self.assertEqual(published.location_footer_message_id, 89)
            location_buttons = bot.send_location.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(len(location_buttons), 3)
            self.assertEqual(
                {row[0].callback_data for row in location_buttons},
                {f"location_label:{order.id}"},
            )
            self.assertEqual(
                telegram_location_url(published),
                "https://t.me/c/4398605075/88",
            )

            published = repo.update(
                published.id,
                second_latitude=41.32,
                second_longitude=69.25,
            )
            bot.send_location.return_value = SimpleNamespace(
                chat_id=-1004398605075,
                message_id=89,
            )
            published = await _publish_location(context, repo, published, 2)
            self.assertEqual(
                telegram_location_url(published, 2),
                "https://t.me/c/4398605075/89",
            )
            self.assertEqual(published.second_location_details_message_id, 90)
            self.assertEqual(published.second_location_footer_message_id, 92)
            self.assertEqual(bot.send_message.await_count, 4)


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

    def test_completed_card_starts_with_a_clear_green_marker(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)

        text = completed_card(order, "15:42")

        self.assertTrue(text.startswith("✅✅✅✅✅✅✅\n✅ "))
        self.assertIn("📦 A7 Pro", text)

    def test_existing_database_gets_delivery_migrations(self):
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        migration_names = set(MIGRATION_COLUMNS)
        legacy_lines = [
            line for line in SCHEMA.splitlines()
            if not any(line.strip().startswith(f"{name} ") for name in migration_names)
        ]
        legacy_schema = "\n".join(legacy_lines).replace(",\n);", "\n);")
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
        migrated = OrderRepository(legacy_path)
        migrated.initialize()
        with migrated.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        self.assertTrue(set(MIGRATION_COLUMNS).issubset(columns))

    def test_seller_is_saved_and_editable(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "seller_name": "Olmas"},
        )
        self.assertEqual(order.seller_name, "Olmas")
        updated = self.repo.transition(order.id, {"draft"}, seller_name="Ali")
        self.assertEqual(updated.seller_name, "Ali")

    def test_payment_status_is_saved_and_editable(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "payment_status": PAID_AT_ASSEMBLY},
        )
        self.assertEqual(order.payment_status, PAID_AT_ASSEMBLY)
        updated = self.repo.transition(order.id, {"draft"}, payment_status="collect_on_delivery")
        self.assertEqual(updated.payment_status, "collect_on_delivery")

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

    def test_location_channel_message_is_saved_and_linked(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "latitude": 41.31, "longitude": 69.24},
        )
        order = self.repo.update(
            order.id,
            location_chat_id=-1004398605075,
            location_message_id=77,
        )
        self.assertEqual(
            telegram_location_url(order),
            "https://t.me/c/4398605075/77",
        )
        keyboard = manager_sent_keyboard(order)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "📍 Локация")

    def test_second_location_is_saved_and_linked(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.31,
                "longitude": 69.24,
                "second_latitude": 41.32,
                "second_longitude": 69.25,
                "second_address_text": "Вторая точка, Ташкент",
            },
        )
        order = self.repo.update(
            order.id,
            location_chat_id=-1004398605075,
            location_message_id=77,
            second_location_chat_id=-1004398605075,
            second_location_message_id=78,
        )
        self.assertEqual(
            telegram_location_url(order, 2),
            "https://t.me/c/4398605075/78",
        )
        self.assertEqual(manager_card(order).count("📍"), 2)
        keyboard = courier_keyboard(order)
        button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("📍 Локация", button_texts)
        self.assertIn("📍 Доп. локация", button_texts)
        self.assertNotIn("🧭 Маршрут", button_texts)
        self.assertNotIn("🗺 Карта", button_texts)
        self.assertNotIn("🗺 Все активные заказы", button_texts)

        order = self.repo.update(order.id, second_location_message_id=79)
        refreshed_keyboard = courier_keyboard(order)
        self.assertEqual(
            refreshed_keyboard.inline_keyboard[0][1].url,
            "https://t.me/c/4398605075/79",
        )

    def test_location_channel_buttons_use_callback_for_basic_group(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "seller_name": "Ali", "address_text": "Яшнабадский район"},
        )
        order = self.repo.update(
            order.id,
            delivery_chat_id=-5125237049,
            delivery_message_id=50,
        )
        keyboard = location_channel_keyboard(order)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].text, "📍 Яшнабадский район")
        self.assertEqual(buttons[1].text, "📦 A7 Pro · №1 · Ali")
        self.assertEqual(buttons[2].text, "📱 +998 90 133 39 99")
        self.assertEqual({button.url for button in buttons}, {None})
        self.assertEqual(
            {button.callback_data for button in buttons},
            {f"location_label:{order.id}"},
        )

    def test_location_channel_buttons_link_directly_for_supergroup(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        order = self.repo.update(
            order.id,
            delivery_chat_id=-1005125237049,
            delivery_message_id=50,
        )
        buttons = [
            button
            for row in location_channel_keyboard(order).inline_keyboard
            for button in row
        ]
        self.assertEqual(
            {button.url for button in buttons},
            {"https://t.me/c/5125237049/50"},
        )
        self.assertTrue(all(button.callback_data is None for button in buttons))

    def test_delivery_confirmation_and_compact_edit_buttons_have_back(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        pending_buttons = [
            button for row in delivery_pending_keyboard(order).inline_keyboard for button in row
        ]
        self.assertTrue(any(button.callback_data == f"undo_complete:{order.id}" for button in pending_buttons))
        completed_button = completed_keyboard(order).inline_keyboard[-1][0]
        self.assertEqual(completed_button.callback_data, f"undo_complete:{order.id}")
        compact = review_keyboard(order.id)
        self.assertEqual(compact.inline_keyboard[0][0].text, "✏️ Изменить")
        expanded = review_keyboard(order.id, expanded=True)
        self.assertTrue(any(button.text == "↩️ Назад" for row in expanded.inline_keyboard for button in row))

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

    def test_cancelled_order_can_be_restored(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        cancelled = self.repo.update(
            order.id,
            status="cancelled",
            courier_id=10,
            courier_name="Courier",
        )
        restored = self.repo.transition(
            cancelled.id,
            {"cancelled"},
            status="pending",
            courier_id=None,
            courier_name=None,
            guard_courier_id=10,
            require_unassigned_or_same=True,
        )
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.courier_id)

    def test_cancelled_keyboards_have_back_button(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        manager_button = manager_cancelled_keyboard(order.id).inline_keyboard[0][0]
        courier_button = courier_cancelled_keyboard(order).inline_keyboard[-1][0]
        self.assertEqual(manager_button.text, "↩️ Назад")
        self.assertEqual(manager_button.callback_data, f"manager_restore:{order.id}")
        self.assertEqual(courier_button.text, "↩️ Назад")
        self.assertEqual(courier_button.callback_data, f"undo_cancel:{order.id}")

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
        map_button = all_locations_keyboard(map_url).inline_keyboard[0][0]
        self.assertEqual(map_button.text, "🗺 Все локации на карте")
        self.assertEqual(map_button.url, map_url)
        self.assertIn("rtext", yandex_route_url(self.repo.get(first.id)))
        self.assertIn("pt", yandex_map_url(self.repo.get(first.id)))

    def test_active_locations_map_numbers_every_location_in_order(self):
        first = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.31,
                "longitude": 69.24,
                "second_latitude": 41.32,
                "second_longitude": 69.25,
                "second_address_text": "Чиланзар",
            },
        )
        second = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "product": "S25 Ultra",
                "latitude": 41.33,
                "longitude": 69.26,
            },
        )
        self.repo.update(first.id, status="pending")
        self.repo.update(second.id, status="pending")

        text, map_url = all_locations_card(self.repo.list_active())

        self.assertIn("📌 <b>1</b> · №1 · основная", text)
        self.assertIn("📌 <b>2</b> · №1 · доп.", text)
        self.assertIn("📌 <b>3</b> · №2 · основная", text)
        self.assertEqual(map_url.count("pm2rdm"), 3)

    def test_active_locations_map_legend_matches_25_point_limit(self):
        for index in range(14):
            order = self.repo.create(
                manager_id=1,
                manager_name="A",
                data={
                    **self.data,
                    "product": f"Model {index + 1}",
                    "latitude": 41.30 + index / 1000,
                    "longitude": 69.20 + index / 1000,
                    "second_latitude": 41.40 + index / 1000,
                    "second_longitude": 69.30 + index / 1000,
                },
            )
            self.repo.update(order.id, status="pending")

        text, map_url = all_locations_card(self.repo.list_active())

        self.assertEqual(map_url.count("pm2rdm"), 25)
        self.assertIn("📌 <b>25</b> · №13 · основная", text)
        self.assertNotIn("· №14 ·", text)
        self.assertIn("показаны 25 из 28 точек", text)

    def test_manager_card_shows_status(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.assertIn("📝 Черновик", manager_card(order))
        completed = self.repo.update(order.id, status="completed")
        self.assertIn("✅ Доставлен", manager_card(completed))

    def test_cards_escape_employee_text(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="<Manager>",
            data={**self.data, "product": "A7 <Pro>", "seller_name": "<Seller>"},
        )
        self.assertIn("A7 &lt;Pro&gt;", manager_card(order))
        self.assertIn("&lt;Seller&gt;", courier_card(order))
        self.assertNotIn("Создал заказ", courier_card(order))
        self.assertNotIn("Получить при доставке", courier_card(order))

    def test_compact_card_hides_empty_time_and_comment_and_keeps_location_last(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "seller_name": "Ali", "address_text": "Чиланзар"},
        )
        card = manager_card(order)
        self.assertNotIn("🕒", card)
        self.assertNotIn("💬", card)
        self.assertGreater(card.index("📍"), card.index("📱"))
        self.assertLess(card.index("📦"), card.index("💰"))

    def test_all_orders_are_listed_for_managers(self):
        first = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        second = self.repo.create(manager_id=2, manager_name="B", data=self.data)
        self.repo.update(first.id, status="completed")
        self.repo.update(second.id, status="cancelled")
        self.assertEqual(
            [order.order_number for order in self.repo.list_all()],
            [second.order_number, first.order_number],
        )

    def test_active_orders_include_drafts_but_hide_closed_orders(self):
        draft = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        pending = self.repo.create(manager_id=2, manager_name="B", data=self.data)
        completed = self.repo.create(manager_id=3, manager_name="C", data=self.data)
        self.repo.update(pending.id, status="pending")
        self.repo.update(completed.id, status="completed")
        self.assertEqual(
            [order.order_number for order in self.repo.list_open()],
            [pending.order_number, draft.order_number],
        )

    def test_address_is_compact(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "address_text": "Дом 1, улица Мукими, Махалля Бунёдкор, Чиланзарский район, Ташкент, Узбекистан",
                "district": "Чиланзарский район",
                "mahalla": "Махалля Бунёдкор",
            },
        )
        value = short_address(order)
        self.assertIn("Махалля Бунёдкор", value)
        self.assertIn("Чиланзарский район", value)
        self.assertNotIn("Узбекистан", value)
        self.assertLessEqual(len(value.split(",")), 3)


if __name__ == "__main__":
    unittest.main()
