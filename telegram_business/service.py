from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .config import BusinessSettings
from .intents import (
    ADDRESS_MARKERS_RE,
    CREDIT_NEGATIVE,
    COORD_RE,
    MAP_URL_RE,
    ORDER_NEGATIVE,
    classify,
    extract_preferred_time,
    extract_text_location,
    is_outside_tashkent,
    normalize as normalize_intent,
    remove_preferred_time,
)
from .language import detect_language
from .products import (
    ExistingGoogleProductRepository,
    ProductMatch,
    extract_product_query,
    format_ambiguous_result,
    format_result,
    model_link_keyboard,
    normalize_model,
    safe_product_url,
)
from .repository import BusinessRepository
from .request_coordinator import NightRequestCoordinator
from .sheets import BusinessSheets
from .telegram_api import TelegramAPIError, TelegramBusinessAPI
from .templates import TEMPLATES, normalize_template_code, render
from .timeutils import is_night, manager_phrases, next_night_end, telegram_datetime

LOG = logging.getLogger("telegram_business")
HANDOFF = {
    "warranty", "return", "complaint", "human_request", "discount",
    "payment", "active_order", "technical", "outside_tashkent",
}
MANDATORY_DETECTION = HANDOFF | {
    "credit", "order_request", "location", "media_only",
}
USER_MESSAGE_FIELDS = {
    "text", "caption", "location", "venue", "contact", "photo", "animation",
    "audio", "document", "paid_media", "sticker", "story", "video",
    "video_note", "voice", "dice", "game", "poll",
}
PRODUCT_HINTS = {
    "iphone", "ipad", "airpods", "macbook", "apple", "samsung", "galaxy",
    "redmi", "xiaomi", "poco", "honor", "huawei", "dyson", "watch",
    "tecno", "infinix", "oppo", "vivo", "realme", "nothing", "oneplus",
    "nokia", "motorola", "asus", "lenovo", "acer", "dell", "msi", "sony", "jbl",
}

_TELEGRAM_HTML_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "span",
}


class _TelegramHTMLValidator(HTMLParser):
    """Small allow-list validator for texts sent with parse_mode=HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.valid = True

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        values = dict(attrs)
        if tag not in _TELEGRAM_HTML_TAGS:
            self.valid = False
            return
        if tag == "a":
            if set(values) != {"href"} or not safe_product_url(values.get("href")):
                self.valid = False
        elif tag == "span":
            if values != {"class": "tg-spoiler"}:
                self.valid = False
        elif attrs and not (
            tag == "blockquote" and values == {"expandable": None}
        ):
            self.valid = False
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if not self.stack or self.stack.pop() != normalized:
            self.valid = False

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.valid = False

    def handle_entityref(self, name: str) -> None:
        if name not in {"amp", "lt", "gt", "quot"}:
            self.valid = False

    def handle_data(self, data: str) -> None:
        if "<" in data or ">" in data:
            self.valid = False

    def handle_charref(self, name: str) -> None:
        try:
            int(name[1:], 16) if name.lower().startswith("x") else int(name)
        except ValueError:
            self.valid = False

    def finish(self) -> bool:
        try:
            self.close()
        except Exception:
            return False
        return self.valid and not self.stack


def _safe_telegram_html(value: str | None) -> bool:
    if not value:
        return False
    parser = _TelegramHTMLValidator()
    try:
        parser.feed(value)
    except Exception:
        return False
    return parser.finish()


@dataclass(frozen=True)
class RuntimePolicy:
    night_start: time
    night_end: time
    manager_start: time
    manager_end: time
    workdays: frozenset[int]
    final_idle_seconds: int
    debounce_seconds: int
    manager_lock_minutes: int
    credit_cooldown_minutes: int
    max_messages_10m: int
    max_messages_session: int


def _runtime_time(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        return fallback


def _runtime_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if minimum <= parsed <= maximum else fallback


def _runtime_workdays(value: Any, fallback) -> frozenset[int]:
    if value is None:
        return frozenset(fallback)
    parts = value if isinstance(value, (tuple, list, set, frozenset)) else str(value).split(",")
    try:
        iso_days = {int(str(part).strip()) for part in parts if str(part).strip()}
    except ValueError:
        return frozenset(fallback)
    if not iso_days or any(day < 1 or day > 7 for day in iso_days):
        return frozenset(fallback)
    return frozenset(day - 1 for day in iso_days)


def _value(row, key: str, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _token_bot_id(token: str) -> str:
    prefix, separator, _ = str(token or "").partition(":")
    return prefix if separator and prefix.isdigit() else ""


def _safe_connection_id(value: object) -> str:
    """Return a stable diagnostic label without exposing the connection ID."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:10]


def sender_type(message: dict, business_user_id: str, bot_id: str = "") -> str:
    """Classify direction without treating an unknown bot as a manager."""
    sender_bot = message.get("sender_business_bot") or {}
    if sender_bot:
        return "business_bot" if bot_id and str(sender_bot.get("id")) == str(bot_id) else "unknown_bot"
    if message.get("is_from_offline") is True:
        return "telegram_auto"
    sender_id = str((message.get("from") or {}).get("id", ""))
    if business_user_id:
        if sender_id != str(business_user_id):
            return "client"
        # Service-only Telegram messages are not a human answer and must not
        # close response-time cycles or activate manager_lock.
        return "manager" if USER_MESSAGE_FIELDS.intersection(message) else "telegram_auto"
    # In a private Business chat the peer's ID equals chat.id. This preserves
    # inbound data safely while a connection update is delayed.
    if sender_id and sender_id == str((message.get("chat") or {}).get("id", "")):
        return "client"
    return "unknown_sender"


def _telegram_location_url(message: dict) -> str | None:
    location = message.get("location")
    if not isinstance(location, dict):
        return None
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return f"https://www.google.com/maps?q={latitude:.6f},{longitude:.6f}"


def _product_text(text: str) -> str:
    """Remove location material without opening any supplied URL."""
    value = MAP_URL_RE.sub(" ", str(text or ""))
    value = COORD_RE.sub(" ", value)
    kept: list[str] = []
    for chunk in re.split(r"[,;\n]+", value):
        address = ADDRESS_MARKERS_RE.search(chunk)
        if not address:
            kept.append(chunk)
            continue
        prefix = chunk[: address.start()]
        address_tail = chunk[address.end() :]
        model_after = re.search(r"\b(?:модель|model)\b", address_tail, re.IGNORECASE)
        if prefix.strip():
            kept.append(prefix)
        if model_after:
            kept.append(address_tail[model_after.start() :])
    return " ".join(" ".join(kept).split())


def _has_product_hint(query: str) -> bool:
    tokens = normalize_model(query).split()
    return bool(
        PRODUCT_HINTS.intersection(tokens)
        or any(any(char.isalpha() for char in token) and any(char.isdigit() for char in token) for token in tokens)
        or (
            any(token.isdigit() for token in tokens)
            and any(token in {"pro", "max", "ultra", "plus", "mini", "note", "air"} for token in tokens)
        )
    )


def _sheet_keyword_match(text: str, term: str) -> bool:
    """Match an editable keyword as a word/phrase, never inside another word."""
    normalized = normalize_intent(term)
    return bool(
        normalized
        and re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text)
    )


class BusinessService:
    def __init__(self, settings: BusinessSettings, clock=None, api=None, products=None):
        self.settings = settings
        self.repo = BusinessRepository(settings.db_path)
        self.clock = clock or (lambda: datetime.now(ZoneInfo(settings.timezone)))
        self.api = api or TelegramBusinessAPI(settings.bot_token)
        self.products = products or ExistingGoogleProductRepository(
            settings.product_price_max_age_minutes,
            getattr(settings, "product_urls_path", None),
        )
        self.sheets = BusinessSheets(
            settings.sheet_id,
            self.repo,
            cache_seconds=settings.template_cache_seconds,
        )
        self.bot_id = str(getattr(settings, "bot_id", "") or _token_bot_id(settings.bot_token))
        self._recent_local: dict[tuple[str, str, str], datetime] = {}
        self.requests = NightRequestCoordinator(self)

    def schedule_order_notification(self, request: Any, now: datetime) -> None:
        destination = str(getattr(self.settings, "orders_chat_id", "") or "").strip()
        request_id = str(_value(request, "request_id", "") or "").strip()
        if not destination or not request_id:
            return
        self.repo.schedule(
            f"request-notify:{request_id}",
            str(_value(request, "chat_id", "")),
            _value(request, "session_id"),
            "request_notify",
            now,
            {"request_id": request_id},
            now,
        )

    @staticmethod
    def _order_notification_text(request: Any, client: Any) -> str:
        fulfillment = str(_value(request, "fulfillment_method", "") or "")
        lines = [
            "🛒 Новая заявка",
            f"Клиент: {str(_value(client, 'first_name', '') or '').strip() or '—'}",
        ]
        username = str(_value(client, "username", "") or "").strip().lstrip("@")
        if username:
            lines.append(f"Telegram: @{username}")
        lines.append(f"ID: {str(_value(request, 'chat_id', '') or '—')}")
        lines.append(f"Модель: {str(_value(request, 'exact_model', '') or '—')}")
        option_kind = str(_value(request, "option_kind", "") or "")
        option_value = str(_value(request, "option_value", "") or "").strip()
        if option_value:
            label = "Размер" if option_kind == "size" else "Память"
            lines.append(f"{label}: {option_value}")
        color = str(_value(request, "color", "") or "").strip()
        lines.append(
            f"Цвет: {color or ('не важен' if _value(request, 'color_any', 0) else '—')}"
        )
        price = str(_value(request, "database_price", "") or "").strip()
        if price:
            try:
                price = f"{int(float(price)):,}".replace(",", " ")
            except (TypeError, ValueError, OverflowError):
                pass
            lines.append(f"Цена в базе: {price} so'm")
        lines.append(
            "Получение: доставка" if fulfillment == "delivery" else "Получение: самовывоз"
        )
        phone = str(_value(request, "phone", "") or "").strip()
        contact_method = str(_value(request, "contact_method", "") or "")
        if phone:
            lines.append(f"Телефон: {phone}")
        elif contact_method == "telegram":
            lines.append("Связь: Telegram")
        location = str(
            _value(request, "location_url", "")
            or _value(request, "address", "")
            or ""
        ).strip()
        if location:
            lines.append(f"Локация: {location}")
        preferred_time = str(_value(request, "preferred_time", "") or "").strip()
        if preferred_time:
            lines.append(f"Удобное время: {preferred_time}")
        return "\n".join(lines)

    def _send_order_notification(self, request_id: str, destination: str, now: datetime) -> None:
        request = self.repo.business_request(request_id)
        if not request or _value(request, "status") != "submitted":
            return
        text = self._order_notification_text(
            request, self.repo.client(str(_value(request, "chat_id", "")))
        )
        delivery_key = f"request-notify:{request_id}"
        decision = self.repo.begin_outbound_delivery(
            delivery_key,
            destination,
            str(_value(request, "session_id", "") or ""),
            "request_notify",
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            now,
        )
        if decision != "send":
            return
        try:
            result = self.api.send_chat_message(destination, text)
        except Exception as exc:
            status = getattr(exc, "status", None)
            retryable = bool(getattr(exc, "retryable", True))
            ambiguous = bool(getattr(exc, "ambiguous", False) or status is None)
            self.repo.finish_outbound_delivery(
                delivery_key,
                now,
                error=exc,
                safe_to_retry=bool(status is not None and retryable and not ambiguous),
                ambiguous=ambiguous,
            )
            raise
        message = result.get("result", {}) if isinstance(result, dict) else {}
        self.repo.finish_outbound_delivery(
            delivery_key,
            now,
            telegram_message_id=message.get("message_id"),
        )
        LOG.info(
            "business_request_notification_sent request_id=%s destination=%s",
            request_id,
            destination,
        )

    def _runtime_policy(self, now: datetime) -> RuntimePolicy:
        try:
            cached = getattr(self.sheets, "settings_cached", None)
            sheet_values: Mapping[str, Any] = (
                cached() if cached else self.sheets.settings(now)
            )
        except Exception:
            sheet_values = {}
        night_start = _runtime_time(sheet_values.get("night_start"), self.settings.night_start)
        night_end = _runtime_time(sheet_values.get("night_end"), self.settings.night_end)
        if night_start <= night_end:
            night_start, night_end = self.settings.night_start, self.settings.night_end
        manager_start = _runtime_time(sheet_values.get("manager_start"), self.settings.manager_start)
        manager_end = _runtime_time(sheet_values.get("manager_end"), self.settings.manager_end)
        if manager_start >= manager_end:
            manager_start, manager_end = self.settings.manager_start, self.settings.manager_end
        return RuntimePolicy(
            night_start=night_start,
            night_end=night_end,
            manager_start=manager_start,
            manager_end=manager_end,
            workdays=_runtime_workdays(
                sheet_values.get("workdays"),
                getattr(self.settings, "workdays", range(7)),
            ),
            final_idle_seconds=_runtime_int(
                sheet_values.get("final_idle_seconds"),
                self.settings.final_idle_seconds,
                1,
                86_400,
            ),
            debounce_seconds=_runtime_int(
                sheet_values.get("debounce_seconds"),
                self.settings.debounce_seconds,
                1,
                60,
            ),
            manager_lock_minutes=_runtime_int(
                sheet_values.get("manager_lock_minutes"),
                self.settings.manager_lock_minutes,
                1,
                10_080,
            ),
            credit_cooldown_minutes=_runtime_int(
                sheet_values.get("credit_cooldown_minutes"),
                self.settings.credit_cooldown_minutes,
                1,
                43_200,
            ),
            max_messages_10m=_runtime_int(
                sheet_values.get("max_bot_messages_10m"),
                self.settings.max_messages_10m,
                1,
                100,
            ),
            max_messages_session=_runtime_int(
                sheet_values.get("max_bot_messages_session"),
                self.settings.max_messages_session,
                1,
                500,
            ),
        )

    @staticmethod
    def _manager_phrases(value: datetime, policy: RuntimePolicy) -> tuple[str, str]:
        return manager_phrases(
            value,
            policy.manager_start,
            policy.workdays,
            policy.night_start,
        )

    def _render_message(
        self,
        code: str,
        language: str,
        now: datetime,
        **values: Any,
    ) -> str | None:
        def one(selected: str) -> str | None:
            try:
                cached = getattr(self.sheets, "render_cached", None)
                if cached:
                    return cached(code, selected, values)
                return self.sheets.render(code, selected, values, now=now)
            except Exception:
                # Cached sheet content must never make Telegram processing fail.
                # Invalid rows fall back to the approved built-in text.
                try:
                    return render(code, selected, **values)
                except (KeyError, RuntimeError, ValueError):
                    return None

        if language == "bi":
            parts = [part for selected in ("ru", "uz") if (part := one(selected))]
            return "\n\n———\n\n".join(parts) if parts else None
        return one(language if language in {"ru", "uz"} else "ru")

    def _classify_runtime(
        self,
        text: str,
        language: str,
        media_only: bool,
        has_location: bool,
        night: bool,
        now: datetime,
    ) -> list[str]:
        fallback = list(dict.fromkeys(classify(text, media_only, has_location)))
        try:
            cached = getattr(self.sheets, "intents_cached", None)
            rows = tuple(cached() if cached else self.sheets.intents(now))
        except Exception:
            rows = ()
        if not rows:
            return fallback

        configured = {row.code: row for row in rows}
        # Sheet keywords extend the deterministic safety classifier. An
        # enabled row must never make mandatory handoff/order detection weaker;
        # setting enabled=false remains an explicit operator kill switch.
        safety_fallback = [
            code
            for code in fallback
            if code in MANDATORY_DETECTION
            or code not in configured
            or configured[code].enabled
        ]
        normalized = normalize_intent(text)
        matched = []
        for row in rows:
            # The schedule controls whether a response may be sent. Intents are
            # still recorded during the day for handoff/order statistics.
            if not row.enabled:
                continue
            negatives = tuple(normalize_intent(term) for term in row.negative_keywords)
            if any(term and term in normalized for term in negatives):
                continue
            if row.code == "media_only":
                is_match = media_only
            elif row.code == "location":
                is_match = has_location
            else:
                terms = (*row.keywords_ru, *row.keywords_uz)
                if row.code in {"credit", "order_request"}:
                    # These two intents cause automatic customer-facing claims,
                    # so editable additions use boundaries and the built-in
                    # negation guard. This still permits a safe custom keyword.
                    is_match = any(
                        _sheet_keyword_match(normalized, term)
                        for term in terms
                        if term
                    )
                    negatives = (
                        CREDIT_NEGATIVE if row.code == "credit" else ORDER_NEGATIVE
                    )
                    if any(normalize_intent(term) in normalized for term in negatives):
                        is_match = False
                else:
                    is_match = any(normalize_intent(term) in normalized for term in terms if term)
                if row.code == "payment" and "payment" not in fallback:
                    # Generic editable words such as "оплата" cannot turn a
                    # harmless full-payment sentence into a sensitive-data
                    # handoff. Built-in approved payment phrases remain active.
                    is_match = False
            if is_match:
                matched.append(row)

        stopping_priorities = [row.priority for row in matched if row.stop_processing]
        if stopping_priorities:
            cutoff = max(stopping_priorities)
            matched = [row for row in matched if row.priority >= cutoff]
        result = list(dict.fromkeys([*safety_fallback, *(row.code for row in matched)]))
        # Editable Sheet keywords may contain the intentionally broad Uzbek
        # new-order word ``buyurtma``.  A deterministic active-order match is
        # authoritative and must route to a human without also recording a new
        # order request.
        if "active_order" in result:
            result = [code for code in result if code != "order_request"]
        return result

    def _intent_rows(self, now: datetime):
        """Return the immutable intent snapshot without production Google I/O."""
        try:
            cached = getattr(self.sheets, "intents_cached", None)
            return tuple(cached() if cached else self.sheets.intents(now))
        except Exception:
            return ()

    def _intent_enabled(self, code: str, now: datetime) -> bool:
        rows = self._intent_rows(now)
        if not rows:
            return True
        row = next((item for item in rows if item.code == code), None)
        return True if row is None else bool(row.enabled)

    def _intent_template(
        self,
        code: str,
        *,
        night: bool,
        now: datetime,
        fallback: str | None = None,
    ) -> str | None:
        """Resolve an enabled response template and enforce its sheet scope."""
        default = normalize_template_code(fallback or code)
        rows = self._intent_rows(now)
        row = next((item for item in rows if item.code == code), None)
        if row is None:
            return default
        if not row.enabled:
            return None
        scope = str(row.scope or "all").strip().lower()
        if scope == "night" and not night:
            return None
        if scope == "day" and night:
            return None
        selected = normalize_template_code(row.template_code or default)
        if selected not in TEMPLATES:
            LOG.warning(
                "invalid_intent_template intent=%s template=%s using=%s",
                code,
                selected,
                default,
            )
            return default
        return selected

    @staticmethod
    def _fits_telegram(text: str | None) -> bool:
        return bool(text) and len(text.encode("utf-16-le")) // 2 <= 4096

    def _product_result_message(
        self,
        match: ProductMatch,
        language: str,
        now: datetime,
    ) -> str | None:
        def one(selected: str) -> str | None:
            fallback = format_result(match, selected)
            parts = fallback.split("\n\n")
            variants = "\n\n".join(parts[1:-1]) if len(parts) >= 3 else ""
            model_name = html.escape(match.models[0][:300])
            if url := safe_product_url(match.url_for(match.models[0])):
                model_name = f'<a href="{html.escape(url, quote=True)}">{model_name}</a>'
            rendered = self._render_message(
                "product_result", selected, now,
                model=model_name, variants=variants,
            )
            return (
                rendered
                if self._fits_telegram(rendered) and _safe_telegram_html(rendered)
                else fallback if rendered is not None else None
            )

        if language == "bi":
            parts = [part for selected in ("ru", "uz") if (part := one(selected))]
            combined = "\n\n———\n\n".join(parts)
            return combined if self._fits_telegram(combined) else format_result(match, "bi") if parts else None
        return one(language if language in {"ru", "uz"} else "ru")

    def _ambiguous_message(
        self,
        match: ProductMatch,
        language: str,
        now: datetime,
    ) -> str | None:
        lines = []
        for number, model in enumerate(match.models[:5], 1):
            safe_model = html.escape(model[:300])
            if url := safe_product_url(match.url_for(model)):
                safe_model = f'<a href="{html.escape(url, quote=True)}">{safe_model}</a>'
            lines.append(f"{number}. {safe_model}")

        def one(selected: str) -> str | None:
            rendered = self._render_message(
                "ambiguous", selected, now, models="\n".join(lines),
            )
            fallback = format_ambiguous_result(match, selected)
            return (
                rendered
                if self._fits_telegram(rendered) and _safe_telegram_html(rendered)
                else fallback if rendered is not None else None
            )

        if language == "bi":
            parts = [part for selected in ("ru", "uz") if (part := one(selected))]
            combined = "\n\n———\n\n".join(parts)
            return combined if self._fits_telegram(combined) else format_ambiguous_result(match, "bi") if parts else None
        return one(language if language in {"ru", "uz"} else "ru")

    def _product_wizard_message(
        self,
        base: str | None,
        step_text: str,
    ) -> str:
        """Add the next selection question without producing invalid HTML.

        Price lists can legitimately approach Telegram's limit. In that rare
        case the approved price result stays visible and the inline button
        labels still identify the next choices; ordinary results include the
        full localized question in the same message.
        """

        if not base:
            return step_text
        combined = f"{base}\n\n{step_text}"
        if self._fits_telegram(combined) and _safe_telegram_html(combined):
            return combined
        return base

    def _connection_allows_reply(self, connection_id: str) -> bool:
        method = getattr(self.repo, "connection_can_reply", None)
        if method:
            try:
                return bool(method(connection_id))
            except TypeError:
                pass
        connection = self.repo.connection(connection_id)
        return bool(connection and _value(connection, "is_enabled", 0) and _value(connection, "can_reply", 0))

    def _repo_session(self, chat_id: str, event_at: datetime):
        policy = self._runtime_policy(event_at)
        try:
            return self.repo.session(chat_id, event_at, policy.night_start, policy.night_end)
        except TypeError:
            return self.repo.session(chat_id, event_at)

    def _session(self, session_id: str, chat_id: str, event_at: datetime):
        method = getattr(self.repo, "session_by_id", None)
        if method:
            row = method(session_id)
            if row is not None:
                return row
        return self._repo_session(chat_id, event_at)

    def _within_reply_window(self, chat_id: str, session_id: str, now: datetime) -> bool:
        method = getattr(self.repo, "within_reply_window", None) or getattr(self.repo, "can_reply_to_chat", None)
        if method:
            for arguments in ((chat_id, now), (chat_id, now, 24)):
                try:
                    return bool(method(*arguments))
                except TypeError:
                    continue
        session = self._session(session_id, chat_id, now)
        raw = _value(session, "last_client_message_at")
        if not raw:
            return False
        try:
            return datetime.fromisoformat(raw) + timedelta(hours=24) >= now
        except (TypeError, ValueError):
            return False

    def _manager_fence_active(
        self, chat_id: str, now: datetime, policy: RuntimePolicy
    ) -> bool:
        method = getattr(self.repo, "manager_fence_active", None)
        if not method:
            return False
        try:
            return bool(method(chat_id, now, policy.manager_lock_minutes))
        except TypeError:
            return bool(method(chat_id, now))

    def _save_message(self, connection_id: str, message: dict, session_id: str, kind: str, now: datetime, update_id: int) -> bool:
        try:
            return bool(self.repo.save_message(connection_id, message, session_id, kind, now, update_id=update_id))
        except TypeError:
            return bool(self.repo.save_message(connection_id, message, session_id, kind, now))

    def _touch_client(self, chat_id: str, session_id: str, now: datetime, event_at: datetime, message_id: int) -> None:
        policy = self._runtime_policy(now)
        try:
            self.repo.touch_client_message(
                chat_id, session_id, now, event_at=event_at, message_id=message_id,
                manager_start=policy.manager_start, manager_end=policy.manager_end,
                workdays=policy.workdays,
            )
        except TypeError:
            self.repo.touch_client_message(chat_id, session_id, event_at)

    def _manager_answer(self, chat_id: str, session_id: str, now: datetime, event_at: datetime, message_id: int) -> None:
        policy = self._runtime_policy(now)
        try:
            self.repo.manager_answer(
                chat_id, now, policy.manager_lock_minutes, event_at=event_at,
                message_id=message_id, session_id=session_id,
                manager_start=policy.manager_start, manager_end=policy.manager_end,
                workdays=policy.workdays,
            )
        except TypeError:
            self.repo.manager_answer(chat_id, event_at, policy.manager_lock_minutes)

    def _record_error(self, operation: str, now: datetime, chat_id: str, session_id: str, exc: Exception) -> None:
        method = getattr(self.repo, "record_error", None) or getattr(self.repo, "log_error", None)
        if method:
            try:
                method(now, "telegram_business", operation, exc, chat_id=chat_id, session_id=session_id)
                return
            except TypeError:
                try:
                    method("telegram_business", operation, chat_id, session_id, type(exc).__name__, str(exc)[:500], now)
                except Exception:
                    LOG.warning("business_error_record_failed")

    def _recent(self, chat_id: str, template: str, discriminator: str, now: datetime, seconds: int) -> bool:
        method = (
            getattr(self.repo, "recent_bot_template", None)
            or getattr(self.repo, "recent_bot_message", None)
            or getattr(self.repo, "bot_message_recent", None)
        )
        if method:
            for kwargs in (
                {"chat_id": chat_id, "template_code": template, "now": now, "cooldown_seconds": seconds, "model_query": discriminator or None},
                {"chat_id": chat_id, "template_code": template, "now": now, "cooldown_seconds": seconds, "discriminator": discriminator},
                {"chat_id": chat_id, "template_code": template, "since": now - timedelta(seconds=seconds), "model_query": discriminator or None},
            ):
                try:
                    if method(**kwargs):
                        return True
                    break
                except TypeError:
                    continue
        last = self._recent_local.get((chat_id, template, discriminator))
        return bool(last and last + timedelta(seconds=seconds) > now)

    def _remember_sent(self, chat_id: str, template: str, discriminator: str, now: datetime) -> None:
        self._recent_local[(chat_id, template, discriminator)] = now

    def process_update(self, update: dict) -> None:
        now = self.clock()
        update_id = update["update_id"]
        update_type = next(
            (
                key
                for key in (
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                    "deleted_business_messages",
                    "callback_query",
                )
                if key in update
            ),
            "unknown",
        )
        LOG.info(
            "business_update_received update_type=%s update_id=%s",
            update_type,
            update_id,
        )
        try:
            if "callback_query" in update:
                self.requests.handle_callback(update, now)
                self.repo.mark_update(update_id, "processed", now)
                return

            if "business_connection" in update:
                event = update["business_connection"]
                if event.get("id") != self.settings.allowed_connection_id:
                    self.repo.mark_update(update_id, "rejected", now)
                    return
                self.repo.upsert_connection(event, now)
                self.repo.mark_update(update_id, "processed", now)
                return

            if "deleted_business_messages" in update:
                event = update["deleted_business_messages"]
                if event.get("business_connection_id") != self.settings.allowed_connection_id:
                    self.repo.mark_update(update_id, "rejected", now)
                    return
                method = getattr(self.repo, "mark_deleted_messages", None)
                if method:
                    method(
                        event.get("business_connection_id", ""),
                        str((event.get("chat") or {}).get("id") or event.get("chat_id") or ""),
                        event.get("message_ids") or (),
                        now,
                    )
                self.repo.mark_update(update_id, "processed", now)
                return

            edited = update.get("edited_business_message")
            if edited is not None:
                connection_id = edited.get("business_connection_id", "")
                if connection_id != self.settings.allowed_connection_id:
                    self.repo.mark_update(update_id, "rejected", now)
                    return
                method = getattr(self.repo, "edit_message", None)
                applied = False
                event_at = telegram_datetime(
                    edited.get("edit_date") or edited.get("date"),
                    now,
                    self.settings.timezone,
                )
                policy = self._runtime_policy(event_at)
                if method:
                    try:
                        applied = bool(method(
                            connection_id,
                            edited,
                            event_at,
                            update_id=update_id,
                            night_start=policy.night_start,
                            night_end=policy.night_end,
                        ))
                    except TypeError:
                        applied = bool(method(connection_id, edited, event_at))
                lookup = getattr(self.repo, "saved_message", None)
                chat = edited.get("chat") or {}
                chat_id = str(chat.get("id") or "")
                message_id = edited.get("message_id")
                saved = (
                    lookup(connection_id, chat_id, int(message_id))
                    if applied and lookup and chat_id and message_id is not None
                    else None
                )
                # Only a new/current revision of an already-persisted incoming
                # client message is a customer automation event. Telegram may
                # also emit edits for manager, Business-bot and offline messages;
                # those stay audited but never create a reply.
                if (
                    saved
                    and chat.get("type") == "private"
                    and saved["sender_type"] == "client"
                    and bool(saved["original_received"])
                    and saved["session_id"]
                ):
                    try:
                        original_at = datetime.fromisoformat(saved["telegram_date"])
                    except (TypeError, ValueError):
                        original_at = event_at
                    manager_covers = getattr(self.repo, "manager_replied_after", None)
                    manager_already_answered = bool(
                        manager_covers
                        and manager_covers(chat_id, original_at, int(message_id))
                    )
                    current_policy = self._runtime_policy(now)
                    if (
                        self._connection_allows_reply(connection_id)
                        and not manager_already_answered
                        and not self._manager_fence_active(
                            chat_id, now, current_policy,
                        )
                    ):
                        debounce_payload = {
                            "connection_id": connection_id,
                            "event_at": event_at.isoformat(),
                            "message_id": int(message_id),
                            "update_id": update_id,
                            "edit_update_id": update_id,
                            "telegram_language_code": (
                                edited.get("from") or {}
                            ).get("language_code"),
                        }
                        durable_debounce = getattr(
                            self.repo, "schedule_debounce", None,
                        )
                        if durable_debounce:
                            durable_debounce(
                                chat_id,
                                saved["session_id"],
                                event_at,
                                now + timedelta(seconds=policy.debounce_seconds),
                                debounce_payload,
                                now,
                                policy.debounce_seconds,
                            )
                        else:
                            self.repo.schedule(
                                f"debounce-edit:{chat_id}:{message_id}:{update_id}",
                                chat_id,
                                saved["session_id"],
                                "debounce",
                                now + timedelta(seconds=policy.debounce_seconds),
                                debounce_payload,
                                now,
                            )
                self.repo.mark_update(update_id, "processed", now)
                return

            message = update.get("business_message")
            if not message:
                self.repo.mark_update(update_id, "ignored", now)
                return
            connection_id = message.get("business_connection_id", "")
            if connection_id != self.settings.allowed_connection_id:
                self.repo.mark_update(update_id, "rejected", now)
                return
            if (message.get("chat") or {}).get("type") != "private":
                self.repo.mark_update(update_id, "ignored", now)
                return

            event_at = telegram_datetime(message.get("date"), now, self.settings.timezone)
            connection = self.repo.connection(connection_id)
            if connection is None:
                remote = self.api.get_business_connection(connection_id)
                if not isinstance(remote, dict) or remote.get("id") != self.settings.allowed_connection_id:
                    raise RuntimeError("Telegram returned an unexpected Business connection")
                self.repo.upsert_connection(remote, now)
                connection = self.repo.connection(connection_id)
                if connection is None:
                    raise RuntimeError("Business connection could not be persisted")
            kind = sender_type(message, _value(connection, "business_user_id", ""), self.bot_id)
            chat_id = str(message["chat"]["id"])
            session = self._repo_session(chat_id, event_at)
            session_id = session["session_id"]
            LOG.info(
                "business_message_classified update_id=%s connection=%s chat_id=%s session_id=%s sender_type=%s",
                update_id,
                _safe_connection_id(connection_id),
                chat_id,
                session_id,
                kind,
            )
            if not self._save_message(connection_id, message, session_id, kind, now, update_id):
                self.repo.mark_update(update_id, "duplicate", now)
                return

            automation_event_at = event_at
            persisted_message = None
            if kind == "client":
                lookup = getattr(self.repo, "saved_message", None)
                if lookup:
                    persisted_message = lookup(
                        connection_id, chat_id, int(message["message_id"]),
                    )
                if persisted_message and persisted_message["edited_at"]:
                    try:
                        revision_at = datetime.fromisoformat(
                            persisted_message["edited_at"]
                        )
                        if revision_at > automation_event_at:
                            automation_event_at = revision_at
                    except (TypeError, ValueError):
                        pass

            if kind == "manager":
                self._manager_answer(chat_id, session_id, now, event_at, int(message["message_id"]))
            elif kind == "client":
                self.repo.upsert_client(chat_id, message.get("from") or {}, now)
                self._touch_client(chat_id, session_id, now, event_at, int(message["message_id"]))
                effective_text = (
                    (persisted_message["text"] or persisted_message["caption"] or "")
                    if persisted_message else
                    (message.get("text") or message.get("caption") or "")
                )
                location_url = (
                    _telegram_location_url(message)
                    or extract_text_location(effective_text)
                )
                preferred_time = extract_preferred_time(effective_text)
                details = {}
                if location_url:
                    details.update(location_received=1, location_url=location_url)
                location = message.get("location") or {}
                outside_location = is_outside_tashkent(
                    effective_text,
                    latitude=location.get("latitude"),
                    longitude=location.get("longitude"),
                )
                if outside_location:
                    # This is only a durable routing marker.  Do not stop the
                    # session here: the debounce action still has to send the
                    # approved handoff message first.
                    details["handoff_reason"] = "outside_tashkent"
                    annotate_one = getattr(self.repo, "annotate_message", None)
                    if annotate_one:
                        annotate_one(
                            connection_id,
                            chat_id,
                            int(message["message_id"]),
                            now,
                            intent="outside_tashkent",
                        )
                if preferred_time:
                    details["preferred_time"] = preferred_time
                if details:
                    self.repo.patch_session(session_id, now, **details)
                manager_covers = getattr(self.repo, "manager_replied_after", None)
                stale_client_event = bool(
                    manager_covers
                    and manager_covers(
                        chat_id, event_at, int(message["message_id"])
                    )
                )
                policy = self._runtime_policy(now)
                lock_covers_event = getattr(
                    self.repo, "manager_lock_covers_event", None,
                )
                event_inside_manager_lock = bool(
                    lock_covers_event
                    and lock_covers_event(chat_id, automation_event_at)
                )
                if (
                    self._connection_allows_reply(connection_id)
                    and not stale_client_event
                    and not event_inside_manager_lock
                    and not self._manager_fence_active(chat_id, now, policy)
                ):
                    # Reset the old final action as soon as the update is
                    # persisted. Otherwise it can fire while this new message
                    # is still waiting for its 2–3 second debounce.
                    current_session = self._session(
                        session_id, chat_id, automation_event_at,
                    )
                    if (
                        bool(_value(current_session, "price_sent", 0))
                        and not bool(_value(current_session, "final_sent", 0))
                    ):
                        self.repo.cancel(f"final:{session_id}")
                        saved_language = _value(self.repo.client(chat_id), "language", "bi") or "bi"
                        self._schedule_final(
                            connection_id, chat_id, session_id,
                            automation_event_at, now,
                            saved_language, policy,
                        )
                    debounce_payload = {
                        "connection_id": connection_id,
                        "event_at": automation_event_at.isoformat(),
                        "message_id": int(message["message_id"]),
                        "update_id": update_id,
                        "telegram_language_code": (message.get("from") or {}).get("language_code"),
                    }
                    if persisted_message and persisted_message["edit_update_id"] is not None:
                        debounce_payload["edit_update_id"] = int(
                            persisted_message["edit_update_id"]
                        )
                    durable_debounce = getattr(self.repo, "schedule_debounce", None)
                    if durable_debounce:
                        durable_debounce(
                            chat_id,
                            session_id,
                            automation_event_at,
                            now + timedelta(seconds=policy.debounce_seconds),
                            debounce_payload,
                            now,
                            policy.debounce_seconds,
                        )
                    else:
                        self.repo.schedule(
                            f"debounce:{chat_id}:{session_id}", chat_id, session_id,
                            "debounce", now + timedelta(seconds=policy.debounce_seconds),
                            debounce_payload, now,
                        )
            self.repo.mark_update(update_id, "processed", now)
        except Exception as exc:
            LOG.error("business_update_failed update_id=%s error_type=%s", update_id, type(exc).__name__)
            self.repo.mark_update(update_id, "error", now, str(exc)[:500])
            # Preserve typed transport metadata (notably Telegram Retry-After)
            # for the durable worker.  Direct callers also get an honest error
            # instead of a silently stuck update.
            raise

    def send(
        self, connection_id: str, chat_id: str, session_id: str, text: str | None,
        template: str, now: datetime, *, parse_mode: str | None = None,
        preview_url: str | None = None, reply_markup: dict | None = None,
        discriminator: str = "", delivery_key: str = "",
        return_message_id: bool = False,
    ) -> bool | int | None:
        if not text:
            return False
        allowed_connection_id = str(
            getattr(self.settings, "allowed_connection_id", "") or ""
        )
        if not allowed_connection_id or str(connection_id) != allowed_connection_id:
            LOG.warning(
                "business_reply_rejected_unallowed_connection connection=%s chat_id=%s",
                _safe_connection_id(connection_id),
                chat_id,
            )
            return False
        policy = self._runtime_policy(now)
        if not self._connection_allows_reply(connection_id):
            return False
        if self._manager_fence_active(chat_id, now, policy):
            return False
        if not self.repo.may_automate(chat_id, now) or not self._within_reply_window(chat_id, session_id, now):
            return False
        session_allowed = getattr(self.repo, "session_may_automate", None)
        if session_allowed and not session_allowed(session_id):
            return False
        ten, session_count = self.repo.bot_message_count(chat_id, session_id, now)
        if ten >= policy.max_messages_10m or session_count >= policy.max_messages_session:
            stop = getattr(self.repo, "stop_session_automation", None)
            if stop:
                stop(session_id, now, "anti_spam_limit")
            else:
                self.repo.patch_session(
                    session_id, now, priority=1, needs_manager_reply=1,
                    handoff_reason="anti_spam_limit", search_disabled=1, status="human_handoff",
                )
            return False
        outbound_key = None
        greeting_templates = {"greeting_model", "greeting_no_model"}
        ledger_template = "greeting" if template in greeting_templates else template
        ledger_scope = ""
        if template in greeting_templates:
            # Both greeting variants are one logical once-per-session reply.
            ledger_scope = session_id
        elif template in {"product_result", "ambiguous"} and (
            delivery_key or discriminator
        ):
            # A catalogue fingerprint is stable for crash recovery, but must
            # not suppress the same legitimate result in a later session.
            ledger_scope = f"{session_id}:{delivery_key or discriminator}"
        elif delivery_key:
            # Telegram message ids are stable event discriminators.  Including
            # the session bounds the durable once-only record to the dialogue
            # cycle in which that event was handled.
            ledger_scope = f"{session_id}:{delivery_key}"
        elif discriminator:
            ledger_scope = f"{session_id}:{discriminator}"
        elif template in {"final", "credit", "human_handoff"}:
            ledger_scope = session_id
        if ledger_scope:
            outbound_key = f"{ledger_template}:{chat_id}:{ledger_scope}"
            begin = getattr(self.repo, "begin_outbound_delivery", None)
            if begin:
                content_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "text": text,
                            "parse_mode": parse_mode,
                            "preview_url": preview_url,
                            "reply_markup": reply_markup,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                decision = begin(
                    outbound_key,
                    chat_id,
                    session_id,
                    template,
                    content_hash,
                    now,
                )
                if decision == "assumed":
                    LOG.warning(
                        "business_reply_assumed_delivered chat_id=%s session_id=%s template=%s",
                        chat_id,
                        session_id,
                        template,
                    )
                    if return_message_id:
                        lookup = getattr(self.repo, "outbound_delivery", None)
                        delivered = lookup(outbound_key) if lookup else None
                        saved_message_id = _value(
                            delivered, "telegram_message_id",
                        )
                        return (
                            int(saved_message_id)
                            if saved_message_id is not None
                            else True
                        )
                    return True
                if decision != "send":
                    return False
        try:
            result = self.api.send_message(
                connection_id, chat_id, text, parse_mode=parse_mode,
                preview_url=preview_url, reply_markup=reply_markup,
            )
        except Exception as exc:
            if outbound_key:
                status = getattr(exc, "status", None)
                retryable = bool(getattr(exc, "retryable", True))
                ambiguous = bool(
                    getattr(exc, "ambiguous", False)
                    or status is None
                )
                safe_retry = bool(
                    status is not None and retryable and not ambiguous
                )
                self.repo.finish_outbound_delivery(
                    outbound_key,
                    now,
                    error=exc,
                    safe_to_retry=safe_retry,
                    ambiguous=ambiguous,
                )
            raise
        raw_message = result.get("result") if isinstance(result, dict) else None
        msg = raw_message if isinstance(raw_message, dict) else {}
        telegram_message_id = msg.get("message_id")
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            error = TelegramAPIError(
                "Telegram returned an invalid sendMessage result",
                status=200,
                retryable=True,
                ambiguous=True,
            )
            if outbound_key:
                self.repo.finish_outbound_delivery(
                    outbound_key,
                    now,
                    error=error,
                    ambiguous=True,
                )
            raise error
        if outbound_key:
            self.repo.finish_outbound_delivery(
                outbound_key,
                now,
                telegram_message_id=telegram_message_id,
            )
        try:
            self.repo.record_bot_message(
                connection_id, chat_id, session_id,
                telegram_message_id, text, template, now,
                model_query=discriminator or None,
            )
        except TypeError:
            self.repo.record_bot_message(
                connection_id, chat_id, session_id,
                telegram_message_id, text, template, now,
            )
        self._remember_sent(chat_id, template, discriminator, now)
        LOG.info(
            "business_reply_sent connection=%s chat_id=%s session_id=%s template=%s telegram_message_id=%s",
            _safe_connection_id(connection_id),
            chat_id,
            session_id,
            template,
            msg.get("message_id", "unknown"),
        )
        return telegram_message_id if return_message_id else True

    def execute(self, action) -> None:
        now = self.clock()
        policy = self._runtime_policy(now)
        payload = json.loads(action["payload"])
        chat_id, session_id = action["chat_id"], action["session_id"]
        if action["action_type"] == "request_notify":
            destination = str(
                getattr(self.settings, "orders_chat_id", "") or ""
            ).strip()
            if not destination:
                return
            self._send_order_notification(
                str(payload.get("request_id") or ""),
                destination,
                now,
            )
            return
        if action["action_type"] == "request_expire":
            # Expiry is a durable state transition and must still happen when
            # a manager lock or permanent pause prevents customer-facing edits.
            self.requests.expire(
                str(payload.get("request_id") or ""),
                str(payload.get("connection_id") or ""),
                now,
            )
            return
        if str(payload.get("connection_id") or "") != str(
            getattr(self.settings, "allowed_connection_id", "") or ""
        ):
            LOG.warning(
                "scheduled_action_rejected_unallowed_connection id=%s chat_id=%s",
                action["action_id"],
                chat_id,
            )
            return
        if not self.repo.may_automate(chat_id, now):
            return
        raw_event_at = payload.get("event_at")
        if raw_event_at:
            try:
                event_at = datetime.fromisoformat(str(raw_event_at))
            except (TypeError, ValueError):
                return
            lock_covers_event = getattr(
                self.repo, "manager_lock_covers_event", None,
            )
            if lock_covers_event and lock_covers_event(chat_id, event_at):
                return
        session_allowed = getattr(self.repo, "session_may_automate", None)
        if session_allowed and not session_allowed(session_id):
            return
        if action["action_type"] == "debounce":
            self._debounce(payload["connection_id"], chat_id, session_id, now, payload)
        elif action["action_type"] == "final":
            self._final(payload["connection_id"], chat_id, session_id, now)
        elif action["action_type"] == "credit":
            language = payload.get("language", "bi")
            template_code = normalize_template_code(
                payload.get("template_code") or "credit"
            )
            if template_code not in TEMPLATES:
                template_code = "credit"
            ru, uz = self._manager_phrases(now, policy)
            sent = self.repo.credit_allowed(chat_id, now, policy.credit_cooldown_minutes) and self.send(
                payload["connection_id"], chat_id, session_id,
                self._render_message(
                    template_code, language, now,
                    manager_time_phrase_ru=ru, manager_time_phrase_uz=uz,
                ),
                template_code, now, delivery_key=str(payload.get("discriminator") or ""),
            )
            if sent:
                self.repo.mark_credit(chat_id, now)
            if payload.get("stop_after_handoff"):
                reason = str(payload.get("handoff_reason") or "human_handoff")
                stop = getattr(self.repo, "stop_session_automation", None)
                if stop:
                    stop(session_id, now, reason)
                if reason == "active_order":
                    pause = getattr(self.repo, "set_bot_paused", None)
                    if pause:
                        pause(chat_id, True, now, "active_order")

    def _burst(
        self,
        rows,
        event_at: datetime,
        policy: RuntimePolicy | None = None,
        anchor_message_id: int | None = None,
    ):
        policy = policy or self._runtime_policy(event_at)
        burst = []
        latest_mode = is_night(event_at, policy.night_start, policy.night_end)
        anchored = []
        for row in rows:
            try:
                row_at = datetime.fromisoformat(row["telegram_date"])
            except (TypeError, ValueError):
                row_at = event_at
            if row_at > event_at:
                continue
            if (
                row_at == event_at
                and anchor_message_id is not None
                and int(row["message_id"]) > int(anchor_message_id)
            ):
                continue
            anchored.append((row, row_at))
        for row, row_at in reversed(anchored):
            if is_night(row_at, policy.night_start, policy.night_end) != latest_mode:
                break
            if burst:
                newer = datetime.fromisoformat(burst[-1]["telegram_date"])
                if (newer - row_at).total_seconds() > policy.debounce_seconds:
                    break
            burst.append(row)
        burst.reverse()
        return burst

    def _schedule_final(
        self, connection_id: str, chat_id: str, session_id: str,
        event_at: datetime, now: datetime, language: str,
        policy: RuntimePolicy | None = None,
    ) -> None:
        policy = policy or self._runtime_policy(now)
        end = next_night_end(event_at, policy.night_end, policy.night_start)
        execute_at = min(event_at + timedelta(seconds=policy.final_idle_seconds), end)
        if execute_at < now:
            execute_at = now
        self.repo.schedule(
            f"final:{session_id}", chat_id, session_id, "final", execute_at,
            {"connection_id": connection_id, "language": language}, now,
        )

    def _debounce(self, connection_id: str, chat_id: str, session_id: str, now: datetime, payload: dict | None = None) -> None:
        # Some embedding/tests construct the service through ``__new__`` for a
        # narrow worker path. Keep the coordinator lazy without weakening the
        # normal constructor wiring.
        if not hasattr(self, "requests"):
            self.requests = NightRequestCoordinator(self)
        payload = payload or {}
        try:
            event_at = datetime.fromisoformat(payload["event_at"]) if payload.get("event_at") else now
        except (TypeError, ValueError):
            event_at = now
        policy = self._runtime_policy(now)
        session = self._session(session_id, chat_id, event_at)
        client = self.repo.client(chat_id)
        all_rows = self.repo.session_messages(session_id)
        try:
            anchor_message_id = int(payload["message_id"]) if payload.get("message_id") is not None else None
        except (TypeError, ValueError):
            anchor_message_id = None
        rows = self._burst(all_rows, event_at, policy, anchor_message_id)
        if not rows:
            return
        try:
            event_at = datetime.fromisoformat(rows[-1]["telegram_date"])
        except (TypeError, ValueError):
            pass
        text = " ".join((row["text"] or row["caption"] or "") for row in rows).strip()
        media_only = bool(rows and not text and rows[-1]["message_type"] not in {"text", "location"})
        first_row_id = rows[0]["id"]
        first_index = next(
            (index for index, row in enumerate(all_rows) if row["id"] == first_row_id),
            0,
        )
        previous = all_rows[:first_index]
        history = " ".join((row["text"] or row["caption"] or "") for row in previous[-8:])
        language, confidence = detect_language(
            text, _value(client, "language"), payload.get("telegram_language_code"), history,
        )
        if language in {"ru", "uz"}:
            self.repo.update_language(chat_id, language, confidence, now)

        telegram_location = any(row["message_type"] == "location" for row in rows)
        text_location_url = extract_text_location(text)
        has_location = telegram_location or bool(text_location_url)
        # The repository already persists Telegram coordinates before debounce.
        # Reuse that URL only for the current Telegram Location burst; a stored
        # address from an earlier message must not finalize every later reply.
        location_url = (
            text_location_url
            or (_value(session, "location_url") if telegram_location else None)
        )
        night = is_night(event_at, policy.night_start, policy.night_end)
        intents = self._classify_runtime(
            text, language, media_only, has_location, night, now,
        )
        active_request = (
            self.requests.active(chat_id, session_id)
            if hasattr(self.repo, "active_business_request")
            else None
        )
        collecting_request = bool(
            active_request
            and _value(active_request, "status") in {"collecting", "ready"}
        )
        current_outside = is_outside_tashkent(text) or any(
            "outside_tashkent" in str(_value(row, "intent", "")).split(";")
            for row in rows
        )
        # An address supplied inside an active draft is data for the manager,
        # not a reason to terminate model selection. We still make no delivery
        # promise; the draft records the outside-Tashkent flag. A standalone
        # delivery question outside Tashkent keeps the mandatory handoff path.
        collect_outside_location = collecting_request or (
            has_location and "delivery" not in intents
        )
        if (
            current_outside
            and not collect_outside_location
            and "outside_tashkent" not in intents
        ):
            intents.append("outside_tashkent")
        elif collect_outside_location:
            intents = [code for code in intents if code != "outside_tashkent"]
        ru, uz = self._manager_phrases(event_at, policy)
        values = {"manager_time_phrase_ru": ru, "manager_time_phrase_uz": uz}

        preferred_time = extract_preferred_time(text)
        query_source = remove_preferred_time(_product_text(text))
        product_query, parsed_memory, parsed_color = extract_product_query(query_source)
        memory = parsed_memory or _value(session, "memory")
        color = parsed_color or _value(session, "color")
        if not product_query and (parsed_memory or parsed_color) and _value(session, "matched_model"):
            # A short follow-up such as "256 black" refines the model already
            # shown in this session; it is not a new model named "256".
            product_query = str(_value(session, "matched_model"))
        if has_location and product_query and not _has_product_hint(product_query):
            product_query = ""
        standalone_safe = {"catalog", "delivery", "pickup", "availability"}.intersection(intents)
        if standalone_safe and product_query and not _has_product_hint(product_query):
            product_query = ""
        if product_query and "product_search" not in intents and not _has_product_hint(product_query):
            recognizes = getattr(self.products, "recognizes_query", None)
            try:
                approved_model_name = bool(recognizes and recognizes(product_query))
            except Exception:
                approved_model_name = False
            # Ordinary conversation is not a failed catalogue search.  This
            # preserves the one-time no-model greeting without consuming the
            # two allowed lookup attempts for messages such as "Как дела?".
            if not approved_model_name:
                product_query = ""
        if product_query:
            if not self._intent_enabled("product_search", now):
                product_query = ""
            elif "product_search" not in intents:
                intents.append("product_search")
        LOG.info(
            "business_message_analyzed chat_id=%s session_id=%s language=%s confidence=%.2f intents=%s model_query=%s",
            chat_id,
            session_id,
            language,
            confidence,
            ",".join(intents) or "none",
            product_query or "none",
        )
        annotate = getattr(self.repo, "annotate_session_messages", None)
        if annotate:
            try:
                annotate(
                    session_id,
                    now,
                    language=language,
                    intent=";".join(intents),
                    model_query=product_query,
                    message_ids=(int(row["message_id"]) for row in rows),
                )
            except TypeError:
                # Compatibility with a repository/fake from the initial
                # rollout; annotation is metadata and must not stop replies.
                pass
        details = {}
        if parsed_memory:
            details["memory"] = parsed_memory
        if parsed_color:
            details["color"] = parsed_color
        if has_location:
            details["location_received"] = 1
            if location_url:
                details["location_url"] = location_url
        if preferred_time:
            details["preferred_time"] = preferred_time
        session_updates = dict(details)
        if product_query:
            # Messages are captured for the manager even during daytime and
            # before the external price source is queried.
            session_updates["model_query"] = product_query
        if session_updates:
            self.repo.patch_session(session_id, now, **session_updates)

        event_delivery_key = f"client-message:{int(rows[-1]['message_id'])}"
        request_screen_cache: list[Any] = []

        def ensure_model_request():
            if request_screen_cache:
                return request_screen_cache[0]
            prepared = None
            if night and hasattr(self.repo, "get_or_create_business_request"):
                prepared = self.requests.begin_model(
                    connection_id=connection_id,
                    chat_id=chat_id,
                    session_id=session_id,
                    language=language,
                    now=now,
                    event_at=event_at,
                    message_id=int(rows[-1]["message_id"]),
                    update_id=payload.get("update_id"),
                    rows=rows,
                    text=text,
                )
            request_screen_cache.append(prepared)
            return prepared

        def bind_request_screen(prepared, sent_result) -> None:
            if (
                not prepared
                or isinstance(sent_result, bool)
                or not isinstance(sent_result, int)
                or sent_result <= 0
            ):
                return
            binder = getattr(self.repo, "bind_business_request_message", None)
            if binder:
                binder(
                    str(_value(prepared.request, "request_id")),
                    int(_value(prepared.request, "revision", 0)),
                    sent_result,
                    now,
                )

        credit = "credit" in intents
        credit_only = credit and not product_query
        handoff = HANDOFF.intersection(intents)
        handoff_reason = sorted(handoff)[0] if handoff else None
        order_request = "order_request" in intents
        if handoff_reason:
            self.repo.patch_session(
                session_id,
                now,
                priority=1,
                needs_manager_reply=1,
                handoff_reason=handoff_reason,
                status="human_handoff",
            )
        if order_request:
            self.repo.patch_session(
                session_id, now, order_intent=1, needs_manager_reply=1,
            )

        def acknowledge_details() -> None:
            if not details:
                return
            ack_allowed = getattr(self.repo, "acknowledgement_allowed", None)
            can_ack = (
                ack_allowed(chat_id, now, 30)
                if ack_allowed
                else not self._recent(chat_id, "data_added", "", now, 30)
            )
            if can_ack and self.send(
                connection_id,
                chat_id,
                session_id,
                self._render_message("data_added", language, now, **values),
                "data_added",
                now,
                delivery_key=event_delivery_key,
            ):
                mark_ack = getattr(self.repo, "mark_acknowledgement", None)
                if mark_ack:
                    mark_ack(chat_id, now)

        price_sent = bool(_value(session, "price_sent", 0))
        final_sent = bool(_value(session, "final_sent", 0))
        expecting_delivery_location = bool(
            collecting_request
            and _value(active_request, "wizard_state") == "delivery_location"
        )
        finalize_for_location = (
            price_sent
            and not final_sent
            and has_location
            and not expecting_delivery_location
        )
        if price_sent and not final_sent:
            self.repo.cancel(f"final:{session_id}")
            if not has_location:
                self._schedule_final(
                    connection_id, chat_id, session_id, event_at, now, language, policy,
                )
                if night:
                    acknowledge_details()

        credit_scheduled = False
        if credit:
            self.repo.patch_session(session_id, now, credit_intent=1)
            credit_template = self._intent_template(
                "credit", night=night, now=now, fallback="credit",
            )
            if credit_template and self.repo.credit_allowed(chat_id, now, policy.credit_cooldown_minutes):
                if night:
                    if self.send(
                        connection_id, chat_id, session_id,
                        self._render_message(credit_template, language, now, **values),
                        credit_template, now, delivery_key=event_delivery_key,
                    ):
                        self.repo.mark_credit(chat_id, now)
                else:
                    self.repo.schedule(
                        f"credit:{chat_id}", chat_id, session_id, "credit", now + timedelta(seconds=10),
                        {
                            "connection_id": connection_id,
                            "language": language,
                            "template_code": credit_template,
                            "discriminator": event_delivery_key,
                            "stop_after_handoff": bool(handoff_reason),
                            "handoff_reason": handoff_reason,
                        },
                        now,
                    )
                    credit_scheduled = True

        if not night:
            if finalize_for_location:
                self._final(connection_id, chat_id, session_id, now)
            if handoff_reason and not credit_scheduled:
                stop = getattr(self.repo, "stop_session_automation", None)
                if stop:
                    stop(session_id, now, handoff_reason)
                else:
                    self.repo.patch_session(
                        session_id, now, search_disabled=1, status="human_handoff",
                    )
                if handoff_reason == "active_order":
                    pause = getattr(self.repo, "set_bot_paused", None)
                    if pause:
                        pause(chat_id, True, now, "active_order")
            return
        if handoff:
            if finalize_for_location:
                self._final(connection_id, chat_id, session_id, now)
            handoff_template = self._intent_template(
                handoff_reason, night=night, now=now,
                fallback="human_handoff",
            )
            self.send(
                connection_id, chat_id, session_id,
                self._render_message(handoff_template, language, now, **values)
                if handoff_template else None,
                handoff_template or "human_handoff", now,
                delivery_key=event_delivery_key,
            )
            stop = getattr(self.repo, "stop_session_automation", None)
            if stop:
                stop(session_id, now, handoff_reason)
            else:
                self.repo.patch_session(
                    session_id, now, priority=1, needs_manager_reply=1,
                    handoff_reason=handoff_reason, search_disabled=1, status="human_handoff",
                )
            if handoff_reason == "active_order":
                pause = getattr(self.repo, "set_bot_paused", None)
                if pause:
                    pause(chat_id, True, now, "active_order")
            return
        if order_request:
            if finalize_for_location:
                self._final(connection_id, chat_id, session_id, now)
            order_template = self._intent_template(
                "order_request", night=night, now=now,
                fallback="order_request",
            )
            self.send(
                connection_id, chat_id, session_id,
                self._render_message(order_template, language, now, **values)
                if order_template else None,
                order_template or "order_request", now,
                delivery_key=event_delivery_key,
            )

        if collecting_request and hasattr(
            self.repo, "active_business_request"
        ):
            handled_request_input = self.requests.handle_expected_input(
                connection_id=connection_id,
                chat_id=chat_id,
                session_id=session_id,
                rows=rows,
                text=text,
                language=language,
                now=now,
                event_at=event_at,
                update_id=payload.get("update_id"),
                message_id=int(rows[-1]["message_id"]),
            )
            if handled_request_input:
                if finalize_for_location:
                    self._final(connection_id, chat_id, session_id, now)
                return
        if finalize_for_location:
            # Location finalization remains mandatory, but runs after any
            # wizard state/render transition so the disclaimer is the last
            # customer-facing message in this burst.
            self._final(connection_id, chat_id, session_id, now)
        if final_sent and details and not product_query:
            if not credit:
                acknowledge_details()
            return
        if finalize_for_location and not product_query:
            return
        if credit_only:
            return
        if bool(_value(session, "search_disabled", 0)):
            return
        standalone_intent = next(
            (
                code
                for code in ("availability", "pickup", "delivery", "catalog")
                if code in intents
            ),
            None,
        )
        if standalone_intent and not product_query:
            request_screen = ensure_model_request()
            if not _value(session, "greeting_sent", 0):
                sent_result = self.send(
                    connection_id, chat_id, session_id,
                    self._render_message("greeting_no_model", language, now, **values),
                    "greeting_no_model", now,
                    reply_markup=(
                        request_screen.reply_markup if request_screen else None
                    ),
                    delivery_key=f"greeting:{session_id}",
                    return_message_id=bool(request_screen),
                )
                if sent_result:
                    bind_request_screen(request_screen, sent_result)
                    self.repo.patch_session(session_id, now, greeting_sent=1)
            template_code = self._intent_template(
                standalone_intent,
                night=night,
                now=now,
                fallback=standalone_intent,
            )
            if template_code:
                sent_result = self.send(
                    connection_id,
                    chat_id,
                    session_id,
                    self._render_message(template_code, language, now, **values),
                    template_code,
                    now,
                    reply_markup=(
                        request_screen.reply_markup
                        if request_screen and _value(session, "greeting_sent", 0)
                        else None
                    ),
                    delivery_key=event_delivery_key,
                    return_message_id=bool(
                        request_screen and _value(session, "greeting_sent", 0)
                    ),
                )
                bind_request_screen(request_screen, sent_result)
            return
        if has_location and not product_query:
            request_screen = ensure_model_request()
            location_template = self._intent_template(
                "location", night=night, now=now,
                fallback="location_before_model",
            )
            sent_result = self.send(
                connection_id, chat_id, session_id,
                self._render_message(location_template, language, now, **values)
                if location_template else None,
                location_template or "location_before_model", now,
                reply_markup=(
                    request_screen.reply_markup if request_screen else None
                ),
                delivery_key=event_delivery_key,
                return_message_id=bool(request_screen),
            )
            bind_request_screen(request_screen, sent_result)
            return
        if media_only:
            request_screen = ensure_model_request()
            media_template = self._intent_template(
                "media_only", night=night, now=now,
                fallback="media_only",
            )
            sent_result = self.send(
                connection_id, chat_id, session_id,
                self._render_message(media_template, language, now, **values)
                if media_template else None,
                media_template or "media_only", now,
                reply_markup=(
                    request_screen.reply_markup if request_screen else None
                ),
                delivery_key=event_delivery_key,
                return_message_id=bool(request_screen),
            )
            bind_request_screen(request_screen, sent_result)
            return

        search_text = product_query
        if text.strip().isdigit():
            choice = self.repo.model_choice(session_id, int(text.strip()))
            if choice:
                search_text = choice["model_name"]
        if not search_text:
            request_screen = ensure_model_request()
            if not _value(session, "greeting_sent", 0):
                sent_result = self.send(
                    connection_id, chat_id, session_id,
                    self._render_message("greeting_no_model", language, now, **values),
                    "greeting_no_model", now,
                    reply_markup=(
                        request_screen.reply_markup if request_screen else None
                    ),
                    delivery_key=f"greeting:{session_id}",
                    return_message_id=bool(request_screen),
                )
                if sent_result:
                    bind_request_screen(request_screen, sent_result)
                    self.repo.patch_session(session_id, now, greeting_sent=1)
            return

        try:
            try:
                match = self.products.search(search_text, memory=memory, color=color)
            except TypeError:
                match = self.products.search(search_text)
        except Exception as exc:
            LOG.error(
                "product_source_unavailable chat_id=%s error_type=%s",
                chat_id, type(exc).__name__,
            )
            self._record_error("product_search", now, chat_id, session_id, exc)
            self.send(
                connection_id, chat_id, session_id,
                self._render_message("product_source_unavailable", language, now, **values),
                "product_source_unavailable", now, delivery_key=event_delivery_key,
            )
            stop = getattr(self.repo, "stop_session_automation", None)
            if stop:
                stop(session_id, now, "product_source_unavailable")
            else:
                self.repo.patch_session(
                    session_id, now, priority=1, needs_manager_reply=1,
                    handoff_reason="product_source_unavailable", search_disabled=1, status="waiting_manager",
                )
            return

        LOG.info(
            "business_product_search chat_id=%s session_id=%s status=%s models=%s",
            chat_id,
            session_id,
            match.status,
            "|".join(match.models[:5]) or "none",
        )

        # Persist what was searched even when the result is ambiguous or not
        # found, so the manager and Sheets see the actual customer request.
        self.repo.patch_session(
            session_id,
            now,
            model_query=search_text,
            memory=memory,
            color=color,
        )

        if not _value(session, "greeting_sent", 0):
            # The customer already supplied something model-like.  A failed
            # lookup must not repeat the long instruction to write a model.
            code = "greeting_model"
            sent_result = self.send(
                connection_id, chat_id, session_id,
                self._render_message(code, language, now, **values),
                code, now, delivery_key=f"greeting:{session_id}",
            )
            if sent_result:
                self.repo.patch_session(session_id, now, greeting_sent=1)

        if match.status == "found" and match.variants:
            self.repo.replace_model_choices(session_id, (), now)
            fingerprint = self._product_fingerprint(match)
            recent_search = getattr(self.repo, "search_was_recent", None)
            duplicate = bool(
                (recent_search and recent_search(session_id, fingerprint, now, 10))
                or self._recent(chat_id, "product_result", fingerprint, now, 600)
            )
            if duplicate:
                return
            request_screen = (
                self.requests.prepare_match(
                    match,
                    connection_id=connection_id,
                    chat_id=chat_id,
                    session_id=session_id,
                    language=language,
                    now=now,
                    event_at=event_at,
                    message_id=int(rows[-1]["message_id"]),
                    update_id=payload.get("update_id"),
                    model_query=search_text,
                    requested_memory=memory,
                    requested_color=color,
                    rows=rows,
                    text=text,
                )
                if hasattr(self.repo, "get_or_create_business_request")
                else None
            )
            product_message = self._product_result_message(match, language, now)
            if request_screen:
                product_message = self._product_wizard_message(
                    product_message,
                    request_screen.step.text,
                )
            sent_result = self.send(
                connection_id, chat_id, session_id,
                product_message,
                "product_result", now,
                parse_mode="HTML",
                reply_markup=(
                    request_screen.reply_markup
                    if request_screen else model_link_keyboard(match)
                ),
                discriminator=fingerprint,
                delivery_key=f"{event_delivery_key}:product:{fingerprint}",
                return_message_id=bool(request_screen),
            )
            if sent_result:
                bind_request_screen(request_screen, sent_result)
                self.repo.patch_session(
                    session_id, now, price_sent=1, matched_model=match.models[0],
                    model_query=search_text, memory=memory, color=color, status="bot_answered",
                )
                mark_search = getattr(self.repo, "mark_search_result", None)
                if mark_search:
                    mark_search(session_id, fingerprint, now)
                self._schedule_final(
                    connection_id, chat_id, session_id, event_at, now, language, policy,
                )
        elif match.status == "ambiguous":
            fingerprint = self._product_fingerprint(match)
            recent_search = getattr(self.repo, "search_was_recent", None)
            duplicate = (
                bool(recent_search and recent_search(session_id, fingerprint, now, 10))
                or self._recent(chat_id, "ambiguous", fingerprint, now, 600)
            )
            if duplicate:
                return
            request_screen = (
                self.requests.prepare_match(
                    match,
                    connection_id=connection_id,
                    chat_id=chat_id,
                    session_id=session_id,
                    language=language,
                    now=now,
                    event_at=event_at,
                    message_id=int(rows[-1]["message_id"]),
                    update_id=payload.get("update_id"),
                    model_query=search_text,
                    requested_memory=memory,
                    requested_color=color,
                    rows=rows,
                    text=text,
                )
                if hasattr(self.repo, "get_or_create_business_request")
                else None
            )
            urls = dict(match.model_urls)
            self.repo.replace_model_choices(session_id, ((model, urls.get(model)) for model in match.models[:5]), now)
            sent_result = self.send(
                connection_id, chat_id, session_id,
                (
                    request_screen.step.text
                    if request_screen else self._ambiguous_message(match, language, now)
                ),
                "ambiguous", now,
                parse_mode="HTML",
                reply_markup=(
                    request_screen.reply_markup
                    if request_screen else model_link_keyboard(match)
                ),
                discriminator=fingerprint,
                delivery_key=f"{event_delivery_key}:ambiguous:{fingerprint}",
                return_message_id=bool(request_screen),
            )
            if sent_result:
                bind_request_screen(request_screen, sent_result)
                mark_search = getattr(self.repo, "mark_search_result", None)
                if mark_search:
                    mark_search(session_id, fingerprint, now)
        elif not bool(_value(session, "search_disabled", 0)):
            failed = int(_value(session, "failed_searches", 0)) + 1
            code = "not_found_1" if failed == 1 else "not_found_2"
            request_screen = ensure_model_request() if failed == 1 else None
            sent_result = self.send(
                connection_id, chat_id, session_id,
                self._render_message(code, language, now, **values),
                code, now,
                reply_markup=(
                    request_screen.reply_markup if request_screen else None
                ),
                delivery_key=event_delivery_key,
                return_message_id=bool(request_screen),
            )
            if sent_result:
                bind_request_screen(request_screen, sent_result)
                self.repo.patch_session(
                    session_id, now, failed_searches=failed, search_disabled=int(failed >= 2),
                    needs_manager_reply=1,
                    handoff_reason="product_not_found" if failed >= 2 else _value(session, "handoff_reason"),
                    status="waiting_manager",
                )

    @staticmethod
    def _product_fingerprint(match: ProductMatch) -> str:
        parts = [
            match.status,
            *match.models,
            *(f"{name}:{url or ''}" for name, url in match.model_urls),
            *(f"{item.memory}:{item.color}:{item.price_uzs}" for item in match.variants),
            f"unmatched:{','.join(match.unmatched_filters)}",
        ]
        # Matched filters are already represented by the rendered variants.
        # Include requested values only when they alter the mismatch notice.
        if match.unmatched_filters:
            parts.extend(
                [
                    f"requested_memory:{match.requested_memory or ''}",
                    f"requested_color:{match.requested_color or ''}",
                ]
            )
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _final(self, connection_id: str, chat_id: str, session_id: str, now: datetime) -> None:
        session = self._session(session_id, chat_id, now)
        if _value(session, "final_sent", 0) or not _value(session, "price_sent", 0):
            return
        policy = self._runtime_policy(now)
        language = _value(self.repo.client(chat_id), "language", "bi") or "bi"
        ru, uz = self._manager_phrases(now, policy)
        if self.send(
            connection_id, chat_id, session_id,
            self._render_message(
                "final", language, now,
                manager_time_phrase_ru=ru, manager_time_phrase_uz=uz,
            ),
            "final", now,
        ):
            self.repo.patch_session(session_id, now, final_sent=1)
