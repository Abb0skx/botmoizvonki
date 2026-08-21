import hashlib
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
USER_AGENT_LIMIT = 500
SOURCE_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")
CRITICAL_CATEGORIES = {"manager", "delivery", "courier", "product", "overall"}


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
    }


def create_review(
    payload: Any,
    *,
    ip_address: str | None,
    user_agent: str | None,
    db_path: Path | str,
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
                ip_hash, user_agent, source, is_delivery_used, needs_attention
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(), data["language"], data["final_comment"],
                data["customer_phone"], ip_digest,
                (user_agent or "")[:USER_AGENT_LIMIT] or None,
                data["source"], data["is_delivery_used"],
                int(data["needs_attention"]),
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
        for code in data["managers"]:
            if code in {"other", "unknown"}:
                manager_rows.append((review_id, None, code))
            else:
                manager_rows.append((review_id, managers_by_code[code], "manager"))
        connection.executemany(
            """
            INSERT INTO review_managers (review_id, manager_id, selection_type)
            VALUES (?, ?, ?)
            """,
            manager_rows,
        )

    data["id"] = review_id
    data["created_at"] = now.isoformat()
    return data


def send_critical_review_notification(review: dict) -> bool:
    if not REVIEWS_TELEGRAM_BOT_TOKEN or not REVIEWS_TELEGRAM_CHAT_ID:
        return False
    lines = ["⚠️ Низкая оценка клиента", ""]
    for category, answer in review["scores"].items():
        label = CATEGORIES[category]["ru"].replace("Как вы оцениваете ", "").rstrip("?")
        lines.append(f"{label}: {'⭐' * answer['rating']}")
    selected_reasons = [
        f"• {REASONS[category][code][0]}"
        for category, codes in review["reasons"].items()
        for code in codes
    ]
    if selected_reasons:
        lines.extend(["", "Причины:", *selected_reasons])
    lines.extend([
        "",
        "Телефон клиента:",
        review["customer_phone"] or "Телефон не указан",
    ])
    if review["final_comment"]:
        lines.extend(["", "Комментарий:", review["final_comment"]])
    lines.extend(["", "Дата:", review["created_at"]])

    response = requests.post(
        f"https://api.telegram.org/bot{REVIEWS_TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": REVIEWS_TELEGRAM_CHAT_ID, "text": "\n".join(lines)},
        timeout=10,
    )
    response.raise_for_status()
    return True
