"""Authoritative monthly Telegram publication plan for Texnikach prices."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalendarPlanEntry:
    day: int
    slot: int
    subposition: int
    requested_label: str
    section_key: str


_PLAN: dict[int, tuple[tuple[str, tuple[str, ...]], ...]] = {
    1: (
        ("Смартфоны — iPhone 13–16 Series", ("smartphones-iphone-13-16",)),
        ("Смартфоны — Xiaomi, Redmi, Poco", ("smartphones-xiaomi-poco",)),
        ("Смартфоны — Infinix", ("smartphones-infinix",)),
    ),
    2: (
        ("Смартфоны — Honor", ("smartphones-honor-huawei",)),
        ("Смартфоны — Samsung", ("smartphones-samsung",)),
        ("Смартфоны — Tecno", ("smartphones-tecno",)),
    ),
    3: (
        ("Смартфоны — Google Pixel", ("smartphones-google-pixel",)),
        ("AirPods, EarPods, HomePod", ("audio-apple",)),
        ("Кнопочные — Nokia / Samsung / Novey", ("smartphones-keypad",)),
    ),
    4: (
        ("Планшеты — iPad", ("tablets-apple",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Планшеты — Honor", ("tablets-honor-huawei",)),
    ),
    5: (
        ("Планшеты — Samsung", ("tablets-samsung",)),
        ("Наушники — Samsung Buds", ("audio-samsung",)),
        ("Наушники — Sony", ("audio-sony",)),
    ),
    6: (
        ("Планшеты — Xiaomi", ("tablets-xiaomi",)),
        ("Apple Watch", ("wearables-apple",)),
        ("Наушники — Huawei", ("audio-huawei-honor",)),
    ),
    7: (
        ("Наушники — Xiaomi Buds", ("audio-xiaomi",)),
        ("Samsung Watch", ("wearables-samsung",)),
        ("Наушники, Колонки — JBL", ("audio-jbl",)),
    ),
    8: (
        ("Наушники — CMF (Nothing)", ("audio-nothing",)),
        ("MacBook, iMac, Mac mini", ("apple-computers-all",)),
        ("Наушники, Колонки — Marshall", ("audio-marshall",)),
    ),
    9: (
        ("Наушники — Anker", ("audio-anker",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Наушники — Beats, Dyson", ("audio-beats-dyson",)),
    ),
    10: (
        ("Колонки — Яндекс", ("audio-yandex",)),
        ("Наушники — Shokz", ("audio-shokz",)),
        ("Микрофоны Hollyland", ("photo-hollyland",)),
    ),
    11: (
        ("Техника DJI", ("photo-dji",)),
        ("Смартфоны — Xiaomi, Redmi, Poco", ("smartphones-xiaomi-poco",)),
        ("Insta360", ("photo-insta360",)),
    ),
    12: (
        ("Xiaomi Watch", ("wearables-xiaomi",)),
        ("Смартфоны — Samsung", ("smartphones-samsung",)),
        ("GoPro, Instax", ("photo-gopro", "photo-instax")),
    ),
    13: (
        ("Huawei Watch", ("wearables-huawei",)),
        ("AirPods, EarPods, HomePod", ("audio-apple",)),
        (
            "Ray-Ban Meta, Oakley Meta",
            ("glasses-ray-ban-meta", "glasses-oakley-meta"),
        ),
    ),
    14: (
        ("CMF Watch (Nothing)", ("wearables-nothing",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Meta Quest", ("vr-meta-quest",)),
    ),
    15: (
        ("PlayStation / Xbox", ("gaming-playstation-xbox",)),
        ("Наушники — Samsung Buds", ("audio-samsung",)),
        ("ТВ-приставки", ("home-tv-boxes",)),
    ),
    16: (
        ("Смартфоны — iPhone 13–16 Series", ("smartphones-iphone-13-16",)),
        ("Apple Watch", ("wearables-apple",)),
        ("Wi-Fi-оборудование", ("home-wifi",)),
    ),
    17: (
        ("Смартфоны — Honor", ("smartphones-honor-huawei",)),
        ("Samsung Watch", ("wearables-samsung",)),
        ("Камеры", ("home-cameras",)),
    ),
    18: (
        ("Смартфоны — Google Pixel", ("smartphones-google-pixel",)),
        ("MacBook, iMac, Mac mini", ("apple-computers-all",)),
        ("Датчики для Яндекс Станции", ("home-yandex-sensors",)),
    ),
    19: (
        ("Планшеты — iPad", ("tablets-apple",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Пылесосы", ("home-vacuums",)),
    ),
    20: (
        ("Планшеты — Samsung", ("tablets-samsung",)),
        ("Очистители / увлажнители воздуха", ("home-air",)),
        ("Адаптеры и USB-кабели", ("charging-adapters-cables",)),
    ),
    21: (
        ("Планшеты — Xiaomi", ("tablets-xiaomi",)),
        ("Смартфоны — Xiaomi, Redmi, Poco", ("smartphones-xiaomi-poco",)),
        ("Car Adapter", ("charging-car",)),
    ),
    22: (
        ("Наушники — Xiaomi Buds", ("audio-xiaomi",)),
        ("Смартфоны — Samsung", ("smartphones-samsung",)),
        ("Power Bank", ("charging-power-bank",)),
    ),
    23: (
        ("Наушники — CMF (Nothing)", ("audio-nothing",)),
        ("AirPods, EarPods, HomePod", ("audio-apple",)),
        ("Зарядные станции", ("charging-stations",)),
    ),
    24: (
        ("Наушники — Anker", ("audio-anker",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Amazfit, Haylou, MiBro", ("wearables-amazfit-haylou-mibro",)),
    ),
    25: (
        ("Колонки — Яндекс", ("audio-yandex",)),
        ("Наушники — Samsung Buds", ("audio-samsung",)),
        ("Porodo — Детские часы", ("wearables-porodo",)),
    ),
    26: (
        ("Техника DJI", ("photo-dji",)),
        ("Apple Watch", ("wearables-apple",)),
        ("Fitbit / Whoop / Whoop remeshog", ("wearables-whoop-fitbit",)),
    ),
    27: (
        ("Xiaomi Watch", ("wearables-xiaomi",)),
        ("Samsung Watch", ("wearables-samsung",)),
        ("Умные кольца — iQibla", ("wearables-iqibla",)),
    ),
    28: (
        ("Huawei Watch", ("wearables-huawei",)),
        ("MacBook, iMac, Mac mini", ("apple-computers-all",)),
        ("Dyson — Фены и стайлеры", ("dyson-hair",)),
    ),
    29: (
        ("CMF Watch (Nothing)", ("wearables-nothing",)),
        ("Смартфоны — iPhone Air / 17 Series", ("smartphones-iphone-air-17",)),
        ("Диктофоны Plaud", ("voice-recorders-plaud",)),
    ),
    30: (
        ("PlayStation / Xbox", ("gaming-playstation-xbox",)),
        ("HDD, SSD, USB, MicroSD", ("storage-all",)),
        (
            "AirTag, SmartTag, Pencil, Keyboard, Mouse",
            (
                "accessories-tags",
                "accessories-pencil",
                "accessories-keyboard",
                "accessories-mouse",
            ),
        ),
    ),
}


def calendar_plan_entries() -> tuple[CalendarPlanEntry, ...]:
    entries: list[CalendarPlanEntry] = []
    for day in range(1, 31):
        for slot, (label, section_keys) in enumerate(_PLAN[day], start=1):
            for subposition, section_key in enumerate(section_keys, start=1):
                entries.append(
                    CalendarPlanEntry(
                        day=day,
                        slot=slot,
                        subposition=subposition,
                        requested_label=label,
                        section_key=section_key,
                    )
                )
    return tuple(entries)


CALENDAR_PLAN_ENTRIES = calendar_plan_entries()
