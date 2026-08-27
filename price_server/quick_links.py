"""Approved Texnikach Telegram quick-link post templates.

The Bot API cannot read arbitrary historic channel messages. These templates
are therefore the authoritative source for both regular link refreshes and the
rotating catalogue published on Tuesday, Thursday and Saturday.
"""

from __future__ import annotations

from typing import Any, Sequence


CATALOG_QUICK_POST_KEY = "quick-index-catalog"
QUICK_LINK_ROTATION_WEEKDAYS = (2, 4, 6)  # ISO: Tue, Thu, Sat.
QUICK_LINK_ROTATION_TIME = "11:00"
QUICK_LINK_ROTATION_ORDER = (
    "quick-index-smartphones",
    "quick-index-tablets",
    "quick-index-audio",
    "quick-index-wearables",
    "quick-index-photo",
    "quick-index-vr",
    "quick-index-home",
    "quick-index-charging",
)


def _target(
    section_key: str,
    message_id: int,
    *,
    link_key: str | None = None,
    section_keys: tuple[str, ...] | None = None,
    initial_message_id: int | None = None,
) -> dict[str, Any]:
    target = {
        "link_key": link_key or section_key,
        "section_keys": list(section_keys or (section_key,)),
        "fallback_url": f"https://t.me/texnikach/{int(message_id)}",
    }
    if initial_message_id is not None:
        target["initial_url"] = (
            f"https://t.me/texnikach/{int(initial_message_id)}"
        )
    return target


def _secondary(
    quick_post_key: str,
    title: str,
    message_id: int,
    links: Sequence[tuple[str, str]],
    targets: Sequence[dict[str, Any]],
    *,
    reconcile_on_install: bool = True,
) -> dict[str, Any]:
    return {
        "quick_post_key": quick_post_key,
        "title": title,
        "message_id": int(message_id),
        "rotating": True,
        "reconcile_on_install": bool(reconcile_on_install),
        "template_html": (
            f"<b>{title}</b>\n\n"
            + "\n\n".join(
                f'• <a href="{{{{post_url:{link_key}}}}}">{label}</a>'
                for link_key, label in links
            )
        ),
        "targets": list(targets),
    }


QUICK_LINK_POST_SPECS: tuple[dict[str, Any], ...] = (
    {
        "quick_post_key": CATALOG_QUICK_POST_KEY,
        "title": "Каталог товаров",
        "message_id": 5050,
        "rotating": True,
        "reconcile_on_install": True,
        "initial_context": {"catalog_date": "26.08.2026"},
        "template_html": (
            "<b>Каталог товаров | {{context:catalog_date}}</b>\n\n"
            "Всё в одном месте — с <b>доставкой</b>\n"
            "Barchasi bir joyda — <b>yetkazish</b> bilan\n\n"
            '▸ <a href="{{quick_post_url:quick-index-smartphones}}">Телефоны</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-tablets}}">Планшеты</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-audio}}">Наушники, колонки</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-wearables}}">Часы, фитнес-браслеты, кольца</a>\n\n'
            '• <a href="{{post_url:apple-computers-all}}">MacBook, iMac, Mac mini</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-photo}}">Фото, видео и блогинг</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-vr}}">VR-очки / Умные очки</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-home}}">Техника для дома и офиса</a>\n\n'
            '▸ <a href="{{quick_post_url:quick-index-charging}}">Зарядные устройства и Power Bank</a>\n\n'
            '• <a href="{{post_url:gaming-playstation-xbox}}">PlayStation / Xbox</a>\n\n'
            '• <a href="{{post_url:dyson-hair}}">Dyson — фены и стайлеры</a>\n\n'
            '• <a href="{{post_url:voice-recorders-plaud}}">Диктофоны Plaud</a>\n\n'
            '• <a href="{{post_url:storage-all}}">HDD, SSD, USB, MicroSD</a>\n\n'
            '• <a href="{{post_url:accessories-combined}}">AirTag, SmartTag, Pencil, Keyboard, Mouse</a>\n\n'
            "————————————\n\n"
            "<b>Курс:</b> 1 $ = 11 930 сум\n\n"
            "<b>Доставка:</b> по городу бесплатно\n"
            "<b>Оплата:</b> после доставки\n"
            '<b>Адрес:</b> Малика, Б2 — <a href="https://t.me/texnikach_info">Локация</a>\n\n'
            "<b>Для заказа</b>\n\n"
            '<a href="https://t.me/Texnikach_Admin">@Texnikach_Admin</a>\n'
            "+998 (99) 844-61-62"
        ),
        "targets": [
            _target("apple-computers-all", 4965),
            _target("gaming-playstation-xbox", 4863),
            _target("dyson-hair", 5021),
            _target("voice-recorders-plaud", 4955),
            _target("storage-all", 4671),
            _target(
                "accessories-tags",
                4824,
                link_key="accessories-combined",
                section_keys=(
                    "accessories-tags",
                    "accessories-pencil",
                    "accessories-keyboard",
                    "accessories-mouse",
                ),
            ),
        ],
    },
    _secondary(
        "quick-index-smartphones", "Смартфоны", 4942,
        (
            ("smartphones-xiaomi-poco", "Xiaomi, Redmi, Poco"),
            ("smartphones-samsung", "Samsung"),
            ("smartphones-iphone-air-17", "iPhone Air / 17 Series"),
            ("smartphones-iphone-13-16", "iPhone 13–16 Series"),
            ("smartphones-honor-huawei", "Honor / Huawei"),
            ("smartphones-google-pixel", "Google Pixel"),
            ("smartphones-infinix", "Infinix"),
            ("smartphones-tecno", "Tecno"),
            ("smartphones-keypad", "Кнопочные — Nokia / Samsung / Novey"),
        ),
        (
            _target("smartphones-xiaomi-poco", 5031),
            _target("smartphones-samsung", 5037),
            _target("smartphones-iphone-air-17", 5041),
            _target("smartphones-iphone-13-16", 5042),
            _target("smartphones-honor-huawei", 4964),
            _target("smartphones-google-pixel", 5024),
            _target("smartphones-infinix", 4992),
            _target("smartphones-tecno", 4944),
            _target("smartphones-keypad", 4904),
        ),
    ),
    _secondary(
        "quick-index-tablets", "Планшеты", 4978,
        (
            ("tablets-apple", "iPad"),
            ("tablets-samsung", "Samsung"),
            ("tablets-xiaomi", "Xiaomi"),
            ("tablets-honor-huawei", "Honor / Huawei"),
        ),
        (
            _target("tablets-apple", 4931),
            _target("tablets-samsung", 5022),
            _target("tablets-xiaomi", 5032),
            _target("tablets-honor-huawei", 4876),
        ),
    ),
    _secondary(
        "quick-index-audio", "Наушники и колонки", 4905,
        (
            ("audio-apple", "AirPods, EarPods, HomePod"),
            ("audio-samsung", "Samsung Buds"),
            ("audio-xiaomi", "Xiaomi Buds"),
            ("audio-sony", "Sony"),
            ("audio-huawei-honor", "Huawei / Honor"),
            ("audio-jbl", "JBL"),
            ("audio-nothing", "CMF (Nothing)"),
            ("audio-marshall", "Marshall"),
            ("audio-anker", "Anker"),
            ("audio-beats-dyson", "Beats / Dyson"),
            ("audio-shokz", "Shokz"),
            ("audio-yandex", "Яндекс"),
        ),
        (
            _target("audio-apple", 5039),
            _target("audio-samsung", 5045),
            _target("audio-xiaomi", 5036),
            _target("audio-sony", 4754),
            _target("audio-huawei-honor", 4868),
            _target("audio-jbl", 4956),
            _target("audio-nothing", 5040),
            _target("audio-marshall", 4960),
            _target("audio-anker", 5043),
            _target("audio-beats-dyson", 4958),
            _target("audio-shokz", 4812),
            _target("audio-yandex", 5046),
        ),
    ),
    _secondary(
        "quick-index-wearables",
        "Часы, фитнес-браслеты и умные кольца",
        4882,
        (
            ("wearables-apple", "Apple Watch"),
            ("wearables-samsung", "Samsung Watch"),
            ("wearables-xiaomi", "Xiaomi Watch"),
            ("wearables-amazfit-haylou-mibro", "Amazfit, Haylou, MiBro"),
            ("wearables-huawei", "Huawei Watch"),
            ("wearables-nothing", "CMF Watch (Nothing)"),
            ("wearables-porodo", "Porodo — детские часы"),
            ("wearables-whoop-fitbit", "Whoop / Fitbit"),
            ("wearables-iqibla", "iQibla"),
        ),
        (
            _target("wearables-apple", 5048),
            _target("wearables-samsung", 5053, initial_message_id=5026),
            _target("wearables-xiaomi", 5054, initial_message_id=5028),
            _target("wearables-amazfit-haylou-mibro", 5029),
            _target("wearables-huawei", 4867),
            _target("wearables-nothing", 4822),
            _target("wearables-porodo", 5044),
            _target("wearables-whoop-fitbit", 5047),
            _target("wearables-iqibla", 5052, initial_message_id=4758),
        ),
        reconcile_on_install=True,
    ),
    _secondary(
        "quick-index-photo", "Фото, видео и блогинг", 4878,
        (
            ("photo-dji", "Техника DJI"),
            ("photo-hollyland", "Микрофоны Hollyland"),
            ("photo-insta360", "Insta360"),
            ("photo-gopro", "GoPro"),
            ("photo-instax", "Instax"),
        ),
        (
            _target("photo-dji", 5049),
            _target("photo-hollyland", 4954),
            _target("photo-insta360", 4940),
            _target("photo-gopro", 4940),
            _target("photo-instax", 4939),
        ),
    ),
    _secondary(
        "quick-index-vr", "VR-очки / Умные очки", 4869,
        (
            ("glasses-ray-ban-meta", "Ray-Ban Meta"),
            ("glasses-oakley-meta", "Oakley Meta"),
            ("vr-meta-quest", "Meta Quest"),
        ),
        (
            _target("glasses-ray-ban-meta", 4753),
            _target("glasses-oakley-meta", 4635),
            _target("vr-meta-quest", 4929),
        ),
    ),
    _secondary(
        "quick-index-home", "Техника для дома и офиса", 5033,
        (
            ("home-tv-boxes", "ТВ-приставки"),
            ("home-wifi", "Wi-Fi-оборудование"),
            ("home-cameras", "Камеры"),
            ("home-yandex-sensors", "Датчики для Яндекс Станции"),
            ("home-vacuums", "Пылесосы"),
            ("home-air", "Очистители / увлажнители воздуха"),
        ),
        (
            _target("home-tv-boxes", 4820),
            _target("home-wifi", 4815),
            _target("home-cameras", 4821),
            _target("home-yandex-sensors", 4761),
            _target("home-vacuums", 4875),
            _target("home-air", 4874),
        ),
    ),
    _secondary(
        "quick-index-charging",
        "Зарядные устройства и Power Bank",
        5016,
        (
            ("charging-adapters-cables", "Адаптеры и USB-кабели"),
            ("charging-car", "Car Adapter"),
            ("charging-power-bank", "Power Bank"),
            ("charging-stations", "Зарядные станции"),
        ),
        (
            _target("charging-adapters-cables", 4903),
            _target("charging-car", 4803),
            _target("charging-power-bank", 5038),
            _target("charging-stations", 4752),
        ),
    ),
)
