"""Approved Texnikach Telegram quick-link post templates.

The Bot API cannot read arbitrary historic channel messages.  These templates
are therefore an explicit, reviewable source for edits of the existing index
posts.  Only ``{{post_url:<link_key>}}`` placeholders are replaced at runtime.
"""

from __future__ import annotations

import re
from typing import Any


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


QUICK_LINK_POST_SPECS: tuple[dict[str, Any], ...] = (
    {
        "quick_post_key": "quick-index-catalog",
        "title": "Каталог товаров",
        "message_id": 5050,
        "template_html": (
            '<tg-emoji emoji-id="5258105663359294787">🗓</tg-emoji>'
            '<b>Каталог товаров | 26.08.2026</b>\n\n'
            'Всё в одном месте - с <b>доставкой</b>\n'
            'Barchasi bir joyda - <b>yetkazish</b> bilan\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/4942">Телефоны</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/4978">Планшеты</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/4905">Наушники, Колонки</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/4882">Часы, Фитнес-браслеты, Кольца</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji> '
            '<a href="{{post_url:apple-computers-all}}">MacBook, '
            '<i><b>🖥</b></i>iMac, Mac mini</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<tg-emoji emoji-id="5258077307985207053">📹</tg-emoji>'
            '<tg-emoji emoji-id="5260652149469094137">🎙</tg-emoji> '
            '<a href="https://t.me/texnikach/4878">Фото, Видео и Блогинг</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<tg-emoji emoji-id="5395440188996466255">🤿</tg-emoji> '
            '<a href="https://t.me/texnikach/4869">VR-очки / '
            '<i><b>🕶️</b></i> Умные очки</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/5033">Техника для дома и офиса</a>\n\n'
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji> '
            '<a href="https://t.me/texnikach/5016">Зарядные устройства и Power Bank</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<tg-emoji emoji-id="6014727481842471406">🎁</tg-emoji> '
            '<a href="{{post_url:gaming-playstation-xbox}}">PlayStation/XBox</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<tg-emoji emoji-id="5433854101613991742">🎀</tg-emoji>'
            '<tg-emoji emoji-id="5271914764899991405">🎁</tg-emoji> '
            '<a href="{{post_url:dyson-hair}}">Dyson Фены и стайлеры</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<a href="{{post_url:voice-recorders-plaud}}">Диктафоны Plaud</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<a href="{{post_url:storage-all}}">HDD, SSD, USB, MicroSD</a>\n\n'
            '<tg-emoji emoji-id="5213362278912506170">▪️</tg-emoji> '
            '<a href="{{post_url:accessories-combined}}">Airtag, SmartTag, '
            'Pencil, Keyboard</a>\n\n'
            + ''.join(
                '<tg-emoji emoji-id="5085015333119460184">👥</tg-emoji>'
                for _ in range(12)
            )
            + '\n\n<b><tg-emoji emoji-id="5258204546391351475">💰</tg-emoji></b>'
            '<b> 1$ = 11 930 So\'m</b>\n\n'
            '• <b>Доставка</b> по городу  <b>Бесплатно</b>!\n'
            '• Оплата <b>после</b> доставки\n'
            '• Малика, <b>Б2</b> (<a href="https://t.me/texnikach_info">Локация</a>)'
            '<tg-emoji emoji-id="5258509201306557640">📍</tg-emoji>\n\n'
            '<b><tg-emoji emoji-id="5258134813302332906">📦</tg-emoji></b>'
            '<b> Для заказа</b>\n'
            '<tg-emoji emoji-id="5258020476977946656">📞</tg-emoji> '
            '<a href="https://t.me/Texnikach_Admin">@Texnikach_Admin</a>\n'
            '<tg-emoji emoji-id="5258337316715373336">🤙</tg-emoji> '
            '+998(99)844-61-62'
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
    {
        "quick_post_key": "quick-index-smartphones",
        "title": "Смартфоны",
        "message_id": 4942,
        "template_html": (
            '<tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji><b>Смартфоны</b>\n\n'
            '<tg-emoji emoji-id="5303379003820744164">📱</tg-emoji> '
            '<a href="{{post_url:smartphones-xiaomi-poco}}">Xiaomi, Redmi, Poco</a>\n\n'
            '<a href="{{post_url:smartphones-samsung}}">Samsung</a>\n\n'
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji> '
            '<a href="{{post_url:smartphones-iphone-air-17}}">iPhone Air / 17 Series</a>\n\n'
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji> '
            '<a href="{{post_url:smartphones-iphone-13-16}}">iPhone 13–16 Series</a>\n\n'
            '<a href="{{post_url:smartphones-honor-huawei}}">Honor</a>\n\n'
            '<tg-emoji emoji-id="5309903664733759148">🏢</tg-emoji> '
            '<a href="{{post_url:smartphones-google-pixel}}">oogle Pixel</a>\n\n'
            '<a href="{{post_url:smartphones-infinix}}">Infinix</a>\n\n'
            '<a href="{{post_url:smartphones-tecno}}">Tecno</a>\n\n'
            '<tg-emoji emoji-id="5255956758077149669">📱</tg-emoji> '
            '<a href="{{post_url:smartphones-keypad}}">Кнопочные \n'
            'Nokia / Samsung / Novey</a>'
        ),
        "targets": [
            _target("smartphones-xiaomi-poco", 5031),
            _target("smartphones-samsung", 5037),
            _target("smartphones-iphone-air-17", 5041),
            _target("smartphones-iphone-13-16", 5042),
            _target("smartphones-honor-huawei", 4964),
            _target("smartphones-google-pixel", 5024),
            _target("smartphones-infinix", 4992),
            _target("smartphones-tecno", 4944),
            _target("smartphones-keypad", 4904),
        ],
    },
    {
        "quick_post_key": "quick-index-tablets",
        "title": "Планшеты",
        "message_id": 4978,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b>Планшеты</b> \n\n'
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji>'
            '<tg-emoji emoji-id="5235967138467955882">🌟</tg-emoji> '
            '<a href="{{post_url:tablets-apple}}">iPad</a>\n\n'
            '<a href="{{post_url:tablets-samsung}}">Samsung</a>\n\n'
            '<tg-emoji emoji-id="5465272191111142019">🧡</tg-emoji> '
            '<a href="{{post_url:tablets-xiaomi}}">Xiaomi</a> \n\n'
            '<a href="{{post_url:tablets-honor-huawei}}">Honor</a>'
        ),
        "targets": [
            _target("tablets-apple", 4931),
            _target("tablets-samsung", 5022),
            _target("tablets-xiaomi", 5032),
            _target("tablets-honor-huawei", 4876),
        ],
    },
    {
        "quick_post_key": "quick-index-audio",
        "title": "Наушники, Колонки",
        "message_id": 4905,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b>Наушники, Колонки\n\n</b>'
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji> '
            '<a href="{{post_url:audio-apple}}">AirPods, EarPods, HomePod</a>\n\n'
            '<a href="{{post_url:audio-samsung}}">Samsung Buds</a>\n\n'
            '<tg-emoji emoji-id="5465272191111142019">🧡</tg-emoji> '
            '<a href="{{post_url:audio-xiaomi}}">Xiaomi Buds</a>\n\n'
            '<tg-emoji emoji-id="5300745368529551374">🌟</tg-emoji> '
            '<a href="{{post_url:audio-sony}}">Sony</a>\n\n'
            '<tg-emoji emoji-id="5303522545922743739">📱</tg-emoji> '
            '<a href="{{post_url:audio-huawei-honor}}">Huawei</a>\n\n'
            '<tg-emoji emoji-id="5275996310976093154">🤩</tg-emoji>'
            '<tg-emoji emoji-id="5364244428481377685">🎵</tg-emoji> '
            '<a href="{{post_url:audio-jbl}}">JBL</a>\n\n'
            '<a href="{{post_url:audio-nothing}}">CMF (Nothing)</a>\n\n'
            '<tg-emoji emoji-id="5366243392160282909">🎧</tg-emoji>'
            '<tg-emoji emoji-id="5364228391073494658">🎵</tg-emoji> '
            '<a href="{{post_url:audio-marshall}}">Marshal</a>\n\n'
            '<a href="{{post_url:audio-anker}}">Anker</a>\n\n'
            '<a href="{{post_url:audio-beats-dyson}}">Beats, Dyson</a> '
            '<tg-emoji emoji-id="5283020601139677158">🔸</tg-emoji>\n\n'
            '<a href="{{post_url:audio-shokz}}">Shokz Спортивные</a> '
            '<tg-emoji emoji-id="5386398294995916342">🤘</tg-emoji>\n\n'
            '<tg-emoji emoji-id="5363928705435447702">🎵</tg-emoji>'
            '<tg-emoji emoji-id="5363968562731954894">🎵</tg-emoji>'
            '<tg-emoji emoji-id="5359811897677848798">🌎</tg-emoji> '
            '<a href="{{post_url:audio-yandex}}">Яндекс</a>'
        ),
        "targets": [
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
        ],
    },
    {
        "quick_post_key": "quick-index-wearables",
        "title": "Часы, Фитнес-браслеты, Умные Кольца",
        "message_id": 4882,
        "reconcile_on_install": True,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b>Часы, Фитнес-браслеты, Умные Кольца </b>\n\n'
            '<tg-emoji emoji-id="5467836213572424390">🍏</tg-emoji> '
            '<a href="{{post_url:wearables-apple}}">Apple Watch</a>\n\n'
            '<a href="{{post_url:wearables-samsung}}">Samsung Watch</a>\n\n'
            '<tg-emoji emoji-id="5465272191111142019">🧡</tg-emoji> '
            '<a href="{{post_url:wearables-xiaomi}}">Xiaomi Watch</a>\n\n'
            '<a href="{{post_url:wearables-amazfit-haylou-mibro}}">Amazfit, Haylou, MiBro</a>\n\n'
            '<tg-emoji emoji-id="5303530036345709045">📱</tg-emoji> '
            '<a href="{{post_url:wearables-huawei}}">Huawei Watch</a>\n\n'
            '<a href="{{post_url:wearables-nothing}}">CMF Watch (Nothing)</a>\n\n'
            '<a href="{{post_url:wearables-porodo}}">Porodo (Детские Часы)</a>\n\n'
            '<a href="{{post_url:wearables-whoop-fitbit}}">Whoop, Fitbit (Браслеты)</a>\n\n'
            '<a href="{{post_url:wearables-iqibla}}">iQibla</a>  '
            '<tg-emoji emoji-id="5247136162266500181">☪️</tg-emoji>'
            '<tg-emoji emoji-id="5283241774775558426">💍</tg-emoji>'
        ),
        "targets": [
            _target("wearables-apple", 5048),
            _target("wearables-samsung", 5053, initial_message_id=5026),
            _target("wearables-xiaomi", 5054, initial_message_id=5028),
            _target("wearables-amazfit-haylou-mibro", 5029),
            _target("wearables-huawei", 4867),
            _target("wearables-nothing", 4822),
            _target("wearables-porodo", 5044),
            _target("wearables-whoop-fitbit", 5047),
            _target("wearables-iqibla", 5052, initial_message_id=4758),
        ],
    },
    {
        "quick_post_key": "quick-index-photo",
        "title": "Фото, Видео и Блогинг",
        "message_id": 4878,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b><tg-emoji emoji-id="5258077307985207053">📹</tg-emoji></b>'
            '<b><tg-emoji emoji-id="5260652149469094137">🎙</tg-emoji></b>'
            '<b>Фото, Видео и Блогинг</b>\n\n'
            '<tg-emoji emoji-id="5273903016930452247">🤐</tg-emoji> '
            '<a href="{{post_url:photo-dji}}">Техника DJI</a>\n\n'
            '<tg-emoji emoji-id="5260652149469094137">🎙</tg-emoji> '
            '<a href="{{post_url:photo-hollyland}}">Микрофоны Hollyland</a>\n\n'
            '<tg-emoji emoji-id="5258205968025525531">📸</tg-emoji> '
            '<a href="{{post_url:photo-insta360}}">Insta360</a>\n\n'
            '<tg-emoji emoji-id="5258077307985207053">📹</tg-emoji> '
            '<a href="{{post_url:photo-gopro}}">GoPro</a>\n\n'
            '<tg-emoji emoji-id="5258205968025525531">📸</tg-emoji> '
            '<a href="{{post_url:photo-instax}}">Instax (моментальные фото)</a>'
        ),
        "targets": [
            _target("photo-dji", 5049),
            _target("photo-hollyland", 4954),
            _target("photo-insta360", 4940),
            _target("photo-gopro", 4940),
            _target("photo-instax", 4939),
        ],
    },
    {
        "quick_post_key": "quick-index-vr",
        "title": "VR-очки / Умные очки",
        "message_id": 4869,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b><tg-emoji emoji-id="5395440188996466255">🤿</tg-emoji></b>'
            '<b> VR-очки / <i><b>🕶️</b></i> Умные очки</b>\n\n'
            '<i><b>🕶️</b></i> <a href="{{post_url:glasses-ray-ban-meta}}">Ray-Ban Meta</a>\n\n'
            '<i><b>🕶️</b></i> <a href="{{post_url:glasses-oakley-meta}}">Oakley Meta</a>\n\n'
            '<tg-emoji emoji-id="5395440188996466255">🤿</tg-emoji> '
            '<a href="{{post_url:vr-meta-quest}}">Meta Quest</a>'
        ),
        "targets": [
            _target("glasses-ray-ban-meta", 4753),
            _target("glasses-oakley-meta", 4635),
            _target("vr-meta-quest", 4929),
        ],
    },
    {
        "quick_post_key": "quick-index-home",
        "title": "Техника для дома и офиса",
        "message_id": 5033,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b>Техника для дома и офиса</b>\n\n'
            '<a href="{{post_url:home-tv-boxes}}">ТВ-приставки</a>\n\n'
            '<a href="{{post_url:home-wifi}}">Wi-Fi-оборудование</a>\n\n'
            '<a href="{{post_url:home-cameras}}">Камеры</a>\n\n'
            '<a href="{{post_url:home-yandex-sensors}}">Датчики для Яндекс Станции</a>\n\n'
            '<a href="{{post_url:home-vacuums}}">Пылесосы</a>\n\n'
            '<a href="{{post_url:home-air}}">Очистители / увлажнители воздуха</a>'
        ),
        "targets": [
            _target("home-tv-boxes", 4820),
            _target("home-wifi", 4815),
            _target("home-cameras", 4821),
            _target("home-yandex-sensors", 4761),
            _target("home-vacuums", 4875),
            _target("home-air", 4874),
        ],
    },
    {
        "quick_post_key": "quick-index-charging",
        "title": "Зарядные устройства и Power Bank",
        "message_id": 5016,
        "template_html": (
            '<b><tg-emoji emoji-id="5116113383128564448">🔥</tg-emoji></b>'
            '<b>Зарядные устройства и Power Bank</b>\n\n'
            '<tg-emoji emoji-id="5366167770671100174">🔌</tg-emoji>'
            '<i><b>🔌</b></i> '
            '<a href="{{post_url:charging-adapters-cables}}">Адаптеры и USB-кабели</a>\n\n'
            '<tg-emoji emoji-id="5233638613358486264">🚗</tg-emoji> '
            '<a href="{{post_url:charging-car}}">Car Adapter</a>\n\n'
            '<tg-emoji emoji-id="5231289012844513283">🔋</tg-emoji> '
            '<a href="{{post_url:charging-power-bank}}">Power Bank</a>\n\n'
            '<tg-emoji emoji-id="5258152182150077732">⚡️</tg-emoji> '
            '<a href="{{post_url:charging-stations}}">Зарядные станции</a>'
        ),
        "targets": [
            _target("charging-adapters-cables", 4903),
            _target("charging-car", 4803),
            _target("charging-power-bank", 5038),
            _target("charging-stations", 4752),
        ],
    },
)


# Telegram restricts custom-emoji entities in channel posts for ordinary bots.
# Keep the same visible emoji characters while using universally supported HTML.
_CUSTOM_EMOJI_RE = re.compile(
    r'<tg-emoji emoji-id="[0-9]+">([^<]*)</tg-emoji>'
)
QUICK_LINK_POST_SPECS = tuple(
    {
        **spec,
        "template_html": _CUSTOM_EMOJI_RE.sub(
            r"\1", str(spec["template_html"])
        ),
    }
    for spec in QUICK_LINK_POST_SPECS
)
