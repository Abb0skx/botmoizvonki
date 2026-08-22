import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .catalog import CATEGORIES, REASONS
from .config import (
    REVIEWS_CRITICAL_RATING,
    REVIEWS_IP_HASH_SECRET,
    REVIEWS_RATE_LIMIT,
    REVIEWS_RATE_WINDOW_SECONDS,
    REVIEWS_TELEGRAM_BOT_TOKEN,
    REVIEWS_TELEGRAM_CHAT_ID,
)
from .database import connect_reviews_db, init_reviews_db


COMMENT_LIMIT = 2000
USER_AGENT_LIMIT = 2000
DEVICE_DATA_LIMIT = 20000
HEADERS_LIMIT = 10000
SOURCE_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")
CRITICAL_CATEGORIES = {"manager", "delivery", "courier", "product", "overall"}
CATEGORY_LABELS = {
    "manager": "Менеджер",
    "price": "Цена",
    "availability": "Наличие",
    "delivery": "Доставка",
    "courier": "Курьер",
    "product": "Товар",
    "overall": "Общая оценка",
}
DEVICE_KEYS = {
    "language", "languages", "timezone", "timezone_offset_minutes",
    "screen_width", "screen_height", "screen_avail_width",
    "screen_avail_height", "color_depth", "pixel_depth",
    "viewport_width", "viewport_height", "device_pixel_ratio",
    "max_touch_points", "touch_supported", "platform",
    "hardware_concurrency", "device_memory_gb", "cookie_enabled",
    "do_not_track", "online", "color_scheme", "reduced_motion",
    "connection", "user_agent_data", "high_entropy",
}


class ReviewValidationError(ValueError):
    pass


class ReviewRateLimitError(ValueError):
    pass


def normalize_phone(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) > 50:
        raise ReviewValidationError("phone_too_long")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:
        digits = "998" + digits
    elif len(digits) == 12 and digits.startswith("998"):
        pass
    else:
        return raw
    return "+" + digits


def hash_ip(ip_address: str | None) -> str | None:
    if not ip_address:
        return None
    return hashlib.sha256(
        f"{REVIEWS_IP_HASH_SECRET}|{ip_address}".encode("utf-8")
    ).hexdigest()


def _clean_comment(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewValidationError(f"{field}_invalid")
    value = value.strip()
    if len(value) > COMMENT_LIMIT:
        raise ReviewValidationError(f"{field}_too_long")
    return value or None


def _sanitize_device_value(value: Any, depth: int = 0) -> Any:
    if depth > 3 or value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_sanitize_device_value(item, depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _sanitize_device_value(item, depth + 1)
            for key, item in list(value.items())[:50]
        }
    return str(value)[:500]


def sanitize_device_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        key: _sanitize_device_value(payload[key])
        for key in DEVICE_KEYS
        if key in payload
    }


def _validate_payload(payload: Any, active_manager_codes: set[str]) -> dict:
    if not isinstance(payload, dict):
        raise ReviewValidationError("invalid_payload")
    if payload.get("website"):
        raise ReviewValidationError("spam_detected")

    language = payload.get("language", "ru")
    if language not in {"ru", "uz"}:
        raise ReviewValidationError("invalid_language")

    source = str(payload.get("source") or "website").lower().strip()
    if not SOURCE_PATTERN.fullmatch(source):
        source = "website"

    delivery_used = payload.get("is_delivery_used")
    if delivery_used not in {True, False, None}:
        raise ReviewValidationError("invalid_delivery_state")

    raw_scores = payload.get("scores") or {}
    raw_reasons = payload.get("reasons") or {}
    if not isinstance(raw_scores, dict) or not isinstance(raw_reasons, dict):
        raise ReviewValidationError("invalid_answers")

    scores: dict[str, dict] = {}
    for category, answer in raw_scores.items():
        if category not in CATEGORIES or not isinstance(answer, dict):
            raise ReviewValidationError("invalid_score")
        rating = answer.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ReviewValidationError("invalid_rating")
        scores[category] = {
            "rating": rating,
            "comment": _clean_comment(answer.get("comment"), f"{category}_comment"),
        }

    reasons: dict[str, list[str]] = {}
    for category, codes in raw_reasons.items():
        if category not in REASONS or not isinstance(codes, list):
            raise ReviewValidationError("invalid_reasons")
        unique_codes: list[str] = []
        for code in codes:
            if not isinstance(code, str) or code not in REASONS[category]:
                raise ReviewValidationError("invalid_reason")
            if code not in unique_codes:
                unique_codes.append(code)
        if unique_codes:
            if category in scores and scores[category]["rating"] == 5:
                raise ReviewValidationError("reasons_not_allowed_for_five")
            reasons[category] = unique_codes

    if delivery_used is False:
        for category in ("delivery", "courier"):
            scores.pop(category, None)
            reasons.pop(category, None)
    elif delivery_used is None and any(
        category in scores or category in reasons
        for category in ("delivery", "courier")
    ):
        delivery_used = True

    raw_managers = payload.get("managers") or []
    if not isinstance(raw_managers, list):
        raise ReviewValidationError("invalid_managers")
    managers: list[str] = []
    for code in raw_managers:
        if not isinstance(code, str):
            raise ReviewValidationError("invalid_manager")
        if code not in active_manager_codes and code not in {"other", "unknown"}:
            raise ReviewValidationError("invalid_manager")
        if code not in managers:
            managers.append(code)

    final_comment = _clean_comment(payload.get("final_comment"), "final_comment")
    phone = normalize_phone(payload.get("customer_phone"))
    has_category_comment = any(answer["comment"] for answer in scores.values())
    if not (scores or reasons or has_category_comment or final_comment or phone):
        raise ReviewValidationError("empty_review")

    needs_attention = any(
        category in CRITICAL_CATEGORIES
        and answer["rating"] <= REVIEWS_CRITICAL_RATING
        for category, answer in scores.items()
    )
    should_notify = (
        any(answer["rating"] < 5 for answer in scores.values())
        or has_category_comment
        or bool(final_comment)
    )
    return {
        "language": language,
        "source": source,
        "is_delivery_used": delivery_used,
        "scores": scores,
        "reasons": reasons,
        "managers": managers,
        "final_comment": final_comment,
        "customer_phone": phone,
        "needs_attention": needs_attention,
        "should_notify": should_notify,
        "device": sanitize_device_payload(payload.get("device")),
    }


def create_review(
    payload: Any,
    *,
    ip_address: str | None,
    user_agent: str | None,
    db_path: Path | str,
    accept_language: str | None = None,
    referer: str | None = None,
    request_headers: dict | None = None,
) -> dict:
    init_reviews_db(db_path)
    ip_digest = hash_ip(ip_address)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=REVIEWS_RATE_WINDOW_SECONDS)).isoformat()

    with connect_reviews_db(db_path) as connection:
        managers_by_code = {
            row["code"]: row["id"]
            for row in connection.execute(
                "SELECT id, code FROM managers WHERE active = 1"
            )
        }
        data = _validate_payload(payload, set(managers_by_code))
        device_json = json.dumps(
            data["device"], ensure_ascii=False, separators=(",", ":")
        )
        if len(device_json) > DEVICE_DATA_LIMIT:
            raise ReviewValidationError("device_data_too_large")
        clean_headers = {
            str(key)[:100]: str(value)[:500]
            for key, value in list((request_headers or {}).items())[:30]
        }
        headers_json = json.dumps(
            clean_headers, ensure_ascii=False, separators=(",", ":")
        )
        if len(headers_json) > HEADERS_LIMIT:
            headers_json = json.dumps(
                {key: value[:200] for key, value in clean_headers.items()},
                ensure_ascii=False,
                separators=(",", ":"),
            )

        if ip_digest:
            recent_count = connection.execute(
                """
                SELECT COUNT(*) FROM reviews
                WHERE ip_hash = ? AND created_at >= ?
                """,
                (ip_digest, cutoff),
            ).fetchone()[0]
            if recent_count >= REVIEWS_RATE_LIMIT:
                raise ReviewRateLimitError("rate_limit")

        cursor = connection.execute(
            """
            INSERT INTO reviews (
                created_at, language, final_comment, customer_phone,
                ip_hash, user_agent, accept_language, referer,
                request_headers_json, device_data_json, device_data_updated_at,
                source, is_delivery_used, needs_attention, should_notify,
                notification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(), data["language"], data["final_comment"],
                data["customer_phone"], ip_digest,
                (user_agent or "")[:USER_AGENT_LIMIT] or None,
                (accept_language or "")[:500] or None,
                (referer or "")[:2000] or None,
                headers_json or None,
                device_json if data["device"] else None,
                now.isoformat() if data["device"] else None,
                data["source"], data["is_delivery_used"],
                int(data["needs_attention"]), int(data["should_notify"]),
                "pending" if data["should_notify"] else "not_required",
            ),
        )
        review_id = cursor.lastrowid

        connection.executemany(
            """
            INSERT INTO review_scores (review_id, category, rating, comment)
            VALUES (?, ?, ?, ?)
            """,
            [
                (review_id, category, answer["rating"], answer["comment"])
                for category, answer in data["scores"].items()
            ],
        )
        connection.executemany(
            """
            INSERT INTO review_reason_selections (review_id, category, reason_code)
            VALUES (?, ?, ?)
            """,
            [
                (review_id, category, code)
                for category, codes in data["reasons"].items()
                for code in codes
            ],
        )
        manager_rows = []
        selected_manager_names = []
        for code in data["managers"]:
            if code in {"other", "unknown"}:
                manager_rows.append((review_id, None, code))
                selected_manager_names.append(
                    "Другой менеджер" if code == "other" else "Не знаю / не помню"
                )
            else:
                manager_rows.append((review_id, managers_by_code[code], "manager"))
                selected_manager_names.append(
                    connection.execute(
                        "SELECT name FROM managers WHERE id = ?",
                        (managers_by_code[code],),
                    ).fetchone()[0]
                )
        connection.executemany(
            """
            INSERT INTO review_managers (review_id, manager_id, selection_type)
            VALUES (?, ?, ?)
            """,
            manager_rows,
        )

    data["id"] = review_id
    data["created_at"] = now.isoformat()
    data["manager_names"] = selected_manager_names
    return data


def send_review_notification(review: dict, db_path: Path | str) -> bool:
    if not REVIEWS_TELEGRAM_BOT_TOKEN or not REVIEWS_TELEGRAM_CHAT_ID:
        with connect_reviews_db(db_path) as connection:
            connection.execute(
                """
                UPDATE reviews SET notification_status = ?, notification_error = ?
                WHERE id = ?
                """,
                ("not_configured", "Telegram variables are not configured", review["id"]),
            )
        return False
    lines = ["⚠️ Новый отзыв клиента", ""]
    for category, answer in review["scores"].items():
        lines.append(f"{CATEGORY_LABELS[category]}: {'⭐' * answer['rating']}")
    if review["manager_names"]:
        lines.extend(["", "Менеджеры: " + ", ".join(review["manager_names"])])
    selected_reasons = [
        f"• {REASONS[category][code][0]}"
        for category, codes in review["reasons"].items()
        for code in codes
    ]
    if selected_reasons:
        lines.extend(["", "Причины:", *selected_reasons])
    category_comments = [
        f"• {CATEGORY_LABELS[category]}: {answer['comment']}"
        for category, answer in review["scores"].items()
        if answer.get("comment")
    ]
    if category_comments:
        lines.extend(["", "Комментарии по категориям:", *category_comments])
    lines.extend([
        "",
        "Телефон клиента:",
        review["customer_phone"] or "Телефон не указан",
    ])
    if review["final_comment"]:
        lines.extend(["", "Общий комментарий:", review["final_comment"]])
    lines.extend([
        "", f"Источник: {review['source']}",
        f"Дата: {review['created_at']}", f"ID: {review['id']}",
    ])

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{REVIEWS_TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": REVIEWS_TELEGRAM_CHAT_ID, "text": "\n".join(lines)[:4096]},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        with connect_reviews_db(db_path) as connection:
            connection.execute(
                """
                UPDATE reviews SET notification_status = ?, notification_error = ?
                WHERE id = ?
                """,
                ("error", type(exc).__name__, review["id"]),
            )
        print("REVIEWS TELEGRAM ERROR:", review["id"], type(exc).__name__)
        return False

    with connect_reviews_db(db_path) as connection:
        connection.execute(
            """
            UPDATE reviews
            SET notification_status = 'sent', notified_at = ?, notification_error = NULL
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), review["id"]),
        )
    print("REVIEWS TELEGRAM SENT:", review["id"])
    return True


# Совместимость с ранним именем функции.
send_critical_review_notification = send_review_notification
