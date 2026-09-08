from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from telegram_business.config import BusinessSettings
from telegram_business.sheets import SHEET_SEEDS
from telegram_business.templates import ALLOWED_PLACEHOLDERS, REVIEW_URL, render


DELIVERY_TEMPLATE_CODES = (
    "delivery_status_pending",
    "delivery_status_picked_up",
    "delivery_status_on_way",
    "delivery_status_completed",
    "delivery_status_cancelled",
)


def enabled_business_env(**changes: str) -> dict[str, str]:
    values = {
        "TELEGRAM_BUSINESS_ENABLED": "true",
        "TELEGRAM_BUSINESS_BOT_TOKEN": "123456:secret",
        "TELEGRAM_BUSINESS_WEBHOOK_SECRET": "safe_secret",
        "TELEGRAM_BUSINESS_ALLOWED_CONNECTION_ID": "connection",
    }
    values.update(changes)
    return values


def test_delivery_notification_config_uses_monitoring_fallbacks():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        BUSINESS_DELIVERY_NOTIFICATIONS_POLL_SECONDS="17",
        BUSINESS_DELIVERY_NOTIFICATIONS_MAX_EVENT_AGE_HOURS="12",
        MONITORING_DELIVERY_BASE_URL="http://delivery-stats:8080/",
        MONITORING_DELIVERY_SERVICE_TOKEN="internal-secret",
    )
    with patch.dict(os.environ, env, clear=True):
        settings = BusinessSettings.load()

    assert settings.delivery_notifications_enabled is True
    assert settings.delivery_notifications_url == "http://delivery-stats:8080"
    assert settings.delivery_notifications_token == "internal-secret"
    assert settings.delivery_notifications_poll_seconds == 17
    assert settings.delivery_notifications_max_event_age_hours == 12
    settings.validate_enabled()


def test_delivery_notification_specific_config_overrides_monitoring_values():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        BUSINESS_DELIVERY_NOTIFICATIONS_URL="https://delivery.internal/base/",
        BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN="business-secret",
        MONITORING_DELIVERY_BASE_URL="http://monitoring-fallback:8080",
        MONITORING_DELIVERY_SERVICE_TOKEN="fallback-secret",
    )
    with patch.dict(os.environ, env, clear=True):
        settings = BusinessSettings.load()

    assert settings.delivery_notifications_url == "https://delivery.internal/base"
    assert settings.delivery_notifications_token == "business-secret"
    settings.validate_enabled()


def test_disabled_delivery_feed_ignores_invalid_unused_poll_value():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="false",
        BUSINESS_DELIVERY_NOTIFICATIONS_POLL_SECONDS="broken",
    )
    with patch.dict(os.environ, env, clear=True):
        settings = BusinessSettings.load()

    assert settings.delivery_notifications_enabled is False
    assert settings.delivery_notifications_poll_seconds == 30
    settings.validate_enabled()


@pytest.mark.parametrize(
    "changes, expected",
    (
        (
            {"BUSINESS_DELIVERY_NOTIFICATIONS_URL": "http://delivery:8080"},
            "service token is missing",
        ),
        (
            {"BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN": "secret"},
            "delivery URL",
        ),
        (
            {
                "BUSINESS_DELIVERY_NOTIFICATIONS_URL": "ftp://delivery/feed",
                "BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN": "secret",
            },
            r"HTTP\(S\) base URL",
        ),
        (
            {
                "BUSINESS_DELIVERY_NOTIFICATIONS_URL": "http://delivery.example.com",
                "BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN": "secret",
            },
            "internal Docker/private host",
        ),
        (
            {
                "BUSINESS_DELIVERY_NOTIFICATIONS_URL": (
                    "https://user:password@delivery.internal/feed?secret=value"
                ),
                "BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN": "secret",
            },
            r"HTTP\(S\) base URL",
        ),
    ),
)
def test_enabled_delivery_feed_rejects_missing_or_unsafe_config(changes, expected):
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        **changes,
    )
    with patch.dict(os.environ, env, clear=True):
        settings = BusinessSettings.load()

    with pytest.raises(RuntimeError, match=expected):
        settings.validate_enabled()


def test_enabled_delivery_feed_rejects_invalid_poll_interval():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        BUSINESS_DELIVERY_NOTIFICATIONS_URL="http://delivery:8080",
        BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN="secret",
        BUSINESS_DELIVERY_NOTIFICATIONS_POLL_SECONDS="0",
    )
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(
            ValueError,
            match="BUSINESS_DELIVERY_NOTIFICATIONS_POLL_SECONDS must be at least 1",
        ):
            BusinessSettings.load()


def test_enabled_delivery_feed_rejects_invalid_event_age():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        BUSINESS_DELIVERY_NOTIFICATIONS_URL="http://delivery:8080",
        BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN="secret",
        BUSINESS_DELIVERY_NOTIFICATIONS_MAX_EVENT_AGE_HOURS="0",
    )
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(
            ValueError,
            match=(
                "BUSINESS_DELIVERY_NOTIFICATIONS_MAX_EVENT_AGE_HOURS "
                "must be at least 1"
            ),
        ):
            BusinessSettings.load()


def test_delivery_flag_is_effectively_off_when_business_integration_is_off():
    with patch.dict(
        os.environ,
        {"BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED": "true"},
        clear=True,
    ):
        settings = BusinessSettings.load()

    assert settings.enabled is False
    assert settings.delivery_notifications_enabled is False


@pytest.mark.parametrize("code", DELIVERY_TEMPLATE_CODES)
@pytest.mark.parametrize("language", ("ru", "uz"))
def test_delivery_status_templates_are_short_and_hide_order_number(code, language):
    text = render(
        code,
        language,
        order_number="1542",
        product="iPhone 16 Pro Max",
        courier_name="Muzrob Oka",
        courier_phone="+998948765070",
    )

    assert "1542" not in text
    assert "{order_number}" not in text
    assert "{" not in text
    assert len(text) <= 240


def test_delivery_status_templates_are_seeded_for_sheets_idempotently():
    rows = SHEET_SEEDS["Автоответы"]
    rows_by_code = {str(row[0]): row for row in rows}

    assert len(rows_by_code) == len(rows)
    assert "order_number" not in ALLOWED_PLACEHOLDERS
    assert {"product", "courier_name", "courier_phone"} <= ALLOWED_PLACEHOLDERS
    for code in DELIVERY_TEMPLATE_CODES:
        row = rows_by_code[code]
        assert row[1] is True
        assert row[2] == "all"
        assert row[6] == 0
        assert "{order_number}" not in row[4]
        assert "{order_number}" not in row[5]


@pytest.mark.parametrize(
    "language, call_to_action",
    (("ru", "оцените нашу работу"), ("uz", "xizmatimizni baholang")),
)
def test_completed_delivery_asks_for_review_once(language, call_to_action):
    text = render(
        "delivery_status_completed",
        language,
        order_number="1542",
        product="iPhone 16 Pro Max",
        courier_name="Muzrob Oka",
        courier_phone="+998948765070",
    )

    assert text.casefold().count(call_to_action) == 1
    assert text.count(REVIEW_URL) == 1


@pytest.mark.parametrize(
    "code",
    tuple(code for code in DELIVERY_TEMPLATE_CODES if code != "delivery_status_completed"),
)
@pytest.mark.parametrize("language", ("ru", "uz"))
def test_review_link_is_not_sent_for_incomplete_delivery_statuses(code, language):
    text = render(
        code,
        language,
        order_number="1542",
        product="iPhone 16 Pro Max",
        courier_name="Muzrob Oka",
        courier_phone="+998948765070",
    )

    assert REVIEW_URL not in text


def test_review_link_is_seeded_only_for_completed_delivery():
    rows = {
        str(row[0]): row
        for row in SHEET_SEEDS["Автоответы"]
        if str(row[0]) in DELIVERY_TEMPLATE_CODES
    }

    for code, row in rows.items():
        expected_count = 1 if code == "delivery_status_completed" else 0
        assert str(row[4]).count(REVIEW_URL) == expected_count
        assert str(row[5]).count(REVIEW_URL) == expected_count


@pytest.mark.parametrize(
    "code, language, expected",
    (
        ("delivery_status_pending", "ru", "⏳ Ожидаем курьера."),
        ("delivery_status_pending", "uz", "⏳ Kuryerni kutyapmiz."),
        (
            "delivery_status_picked_up",
            "ru",
            "📦 Курьер Muzrob Oka забрал товар.",
        ),
        (
            "delivery_status_picked_up",
            "uz",
            "📦 Kuryer Muzrob Oka mahsulotni olib ketdi.",
        ),
        (
            "delivery_status_on_way",
            "ru",
            "🚗 Курьер Muzrob Oka выехал. Пожалуйста, будьте по указанному адресу и готовы "
            "получить товар.\n\nТелефон курьера: +998948765070",
        ),
        (
            "delivery_status_on_way",
            "uz",
            "🚗 Kuryer Muzrob Oka yo‘lga chiqdi. Iltimos, ko‘rsatilgan manzilda bo‘ling va "
            "mahsulotni qabul qilishga tayyor turing.\n\n"
            "Kuryer raqami: +998948765070",
        ),
        (
            "delivery_status_completed",
            "ru",
            "✅ Товар доставлен.\n\nПожалуйста, оцените нашу работу: " + REVIEW_URL,
        ),
        (
            "delivery_status_completed",
            "uz",
            "✅ Mahsulot yetkazildi.\n\nIltimos, xizmatimizni baholang: "
            + REVIEW_URL,
        ),
        (
            "delivery_status_cancelled",
            "ru",
            "❌ Доставка отменена. Подробности уточнит менеджер.",
        ),
        (
            "delivery_status_cancelled",
            "uz",
            "❌ Yetkazib berish bekor qilindi. Tafsilotlarni menejer "
            "aniqlashtiradi.",
        ),
    ),
)
def test_delivery_status_copy_is_exact(code, language, expected):
    assert render(
        code,
        language,
        courier_name="Muzrob Oka",
        courier_phone="+998948765070",
    ) == expected


def test_delivery_courier_phone_is_not_a_global_business_setting():
    env = enabled_business_env(
        BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED="true",
        BUSINESS_DELIVERY_NOTIFICATIONS_URL="http://delivery:8080",
        BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN="secret",
        BUSINESS_DELIVERY_COURIER_PHONE="+998000000000",
    )
    with patch.dict(os.environ, env, clear=True):
        settings = BusinessSettings.load()

    settings.validate_enabled()
    assert not hasattr(settings, "delivery_courier_phone")
