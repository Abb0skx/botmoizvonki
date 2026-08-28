from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import requests


CALLBACK_DATA_PREFIX = "nr1:"
_CALLBACK_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,60}$")
_MAX_INLINE_BUTTONS = 100


def make_callback_data(token: str) -> str:
    """Build an opaque, PII-free Telegram callback payload.

    Telegram limits callback_data to 64 bytes.  The server stores the action and
    values behind this random token; no chat id, model, phone or location is put
    into the button itself.
    """
    if not isinstance(token, str) or _CALLBACK_TOKEN.fullmatch(token) is None:
        raise ValueError("callback token must be opaque base64url data")
    value = f"{CALLBACK_DATA_PREFIX}{token}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("callback data exceeds Telegram's 64-byte limit")
    return value


def parse_callback_data(value: Any) -> str | None:
    """Return the opaque token for a valid night-request callback."""
    if not isinstance(value, str) or not value.startswith(CALLBACK_DATA_PREFIX):
        return None
    if len(value.encode("utf-8")) > 64:
        return None
    token = value[len(CALLBACK_DATA_PREFIX):]
    return token if _CALLBACK_TOKEN.fullmatch(token) is not None else None


def normalize_inline_keyboard(reply_markup: Any) -> dict[str, Any] | None:
    """Validate and copy the only reply-markup shape Business replies allow.

    Native reply keyboards can request a phone/contact/location and remain
    visible after a restart.  The night wizard deliberately supports only
    inline HTTPS links and opaque ``nr1:`` callbacks.
    """
    if reply_markup is None:
        return None
    if not isinstance(reply_markup, dict) or set(reply_markup) != {"inline_keyboard"}:
        raise ValueError("only InlineKeyboardMarkup is allowed")
    rows = reply_markup.get("inline_keyboard")
    if not isinstance(rows, list):
        raise ValueError("inline_keyboard must be a list")

    normalized: list[list[dict[str, str]]] = []
    button_count = 0
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("inline keyboard rows must be non-empty lists")
        normalized_row: list[dict[str, str]] = []
        for button in row:
            button_count += 1
            if button_count > _MAX_INLINE_BUTTONS:
                raise ValueError("inline keyboard has too many buttons")
            if not isinstance(button, dict):
                raise ValueError("inline keyboard button must be an object")
            text = button.get("text")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 128
                or "\x00" in text
            ):
                raise ValueError("inline keyboard button has invalid text")
            action_keys = set(button) - {"text"}
            if action_keys == {"callback_data"}:
                callback_data = button.get("callback_data")
                if parse_callback_data(callback_data) is None:
                    raise ValueError("inline callback_data must be an opaque nr1 token")
                normalized_row.append(
                    {"text": text, "callback_data": callback_data}
                )
                continue
            if action_keys == {"url"}:
                target = button.get("url")
                parsed = urlparse(target) if isinstance(target, str) else None
                if (
                    parsed is None
                    or parsed.scheme.lower() != "https"
                    or not parsed.netloc
                    or len(target) > 2048
                ):
                    raise ValueError("inline keyboard URL must use HTTPS")
                normalized_row.append({"text": text, "url": target})
                continue
            # This also rejects request_contact/request_location, web apps,
            # payments, login URLs and every ReplyKeyboardMarkup-only field.
            raise ValueError("inline keyboard button action is not allowed")
        normalized.append(normalized_row)
    return {"inline_keyboard": normalized}


class TelegramAPIError(RuntimeError):
    """A log-safe Telegram failure with scheduler-friendly retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
        ambiguous: bool = False,
    ):
        safe_message = str(message or "Telegram API request failed").replace("\n", " ")[:300]
        super().__init__(safe_message)
        self.status = status
        self.status_code = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.ambiguous = bool(ambiguous)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class TelegramBusinessAPI:
    def __init__(self, token: str, *, http=None, timeout: float = 15):
        self.token = token
        self.http = http or requests
        self.timeout = timeout

    def _safe_description(self, value: Any) -> str:
        text = str(value or "Telegram API request failed").replace("\n", " ")
        if self.token:
            text = text.replace(self.token, "[redacted]")
        text = re.sub(r"/bot[^/\s]+/", "/bot[redacted]/", text)
        return text[:300]

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict:
        # The URL containing the secret token is never attached to raised errors.
        endpoint = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            response = self.http.post(endpoint, json=payload or {}, timeout=self.timeout)
        except requests.RequestException:
            raise TelegramAPIError(
                "Telegram network request failed",
                retryable=True,
                ambiguous=True,
            ) from None

        body: dict[str, Any] = {}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                body = decoded
        except (TypeError, ValueError):
            body = {}

        http_status = int(getattr(response, "status_code", 0) or 0)
        telegram_status = _positive_int(body.get("error_code"))
        status = telegram_status or http_status or None
        parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry_after = _positive_int(parameters.get("retry_after"))
        if retry_after is None:
            headers = getattr(response, "headers", {}) or {}
            retry_after = _positive_int(headers.get("Retry-After"))

        if http_status == 429 or telegram_status == 429:
            raise TelegramAPIError(
                "Telegram rate limit reached",
                status=429,
                retryable=True,
                retry_after=retry_after,
            )
        if not 200 <= http_status < 300 or body.get("ok") is False:
            retryable = (
                http_status >= 500
                or http_status in {408, 425}
                or telegram_status is not None and telegram_status >= 500
            )
            # For a mutating request, an HTTP timeout/server response does not
            # prove that Telegram failed before committing the message.  Never
            # retry such an outcome as definitely unsent; the outbound ledger
            # deliberately chooses at-most-once delivery.  Telegram's explicit
            # 429 branch above remains the one safe sendMessage retry.
            ambiguous = method == "sendMessage" and (
                http_status == 408 or http_status >= 500
            )
            description = self._safe_description(body.get("description"))
            raise TelegramAPIError(
                description,
                status=status,
                retryable=retryable,
                retry_after=retry_after,
                ambiguous=ambiguous,
            )
        if body.get("ok") is not True:
            raise TelegramAPIError(
                "Telegram returned an invalid response",
                status=status,
                retryable=True,
                ambiguous=True,
            )
        return body

    def get_me(self) -> dict:
        return self._call("getMe").get("result") or {}

    def get_business_connection(self, connection_id: str) -> dict:
        return self._call(
            "getBusinessConnection",
            {"business_connection_id": connection_id},
        ).get("result") or {}

    @staticmethod
    def _message_result(response: dict, method: str) -> dict:
        result = response.get("result") if isinstance(response, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise TelegramAPIError(
                f"Telegram returned an invalid {method} result",
                status=200,
                retryable=True,
                # Retrying an identical edit is semantically idempotent.  A
                # malformed send success is not, and is overridden below.
                ambiguous=method == "sendMessage",
            )
        return result

    @staticmethod
    def _message_target(
        connection_id: str, chat_id: str, message_id: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ValueError("business_connection_id is required")
        if chat_id is None or isinstance(chat_id, bool) or not str(chat_id).strip():
            raise ValueError("chat_id is required")
        payload: dict[str, Any] = {
            "business_connection_id": connection_id,
            "chat_id": str(chat_id),
        }
        if message_id is not None:
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or message_id <= 0
            ):
                raise ValueError("message_id must be a positive integer")
            payload["message_id"] = message_id
        return payload

    @staticmethod
    def _link_preview(preview_url: str | None) -> dict[str, Any]:
        if not preview_url:
            return {"is_disabled": True}
        parsed = urlparse(preview_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("link preview URL must use HTTPS")
        return {
            "is_disabled": False,
            "url": preview_url,
            "prefer_large_media": True,
            "show_above_text": True,
        }

    def send_message(
        self,
        connection_id: str,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        preview_url: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        # None of the reply/edit/callback methods marks a Business message read.
        payload = self._message_target(connection_id, chat_id)
        payload.update(
            text=text,
            link_preview_options=self._link_preview(preview_url),
        )
        if parse_mode:
            payload["parse_mode"] = parse_mode
        normalized_markup = normalize_inline_keyboard(reply_markup)
        if normalized_markup is not None:
            payload["reply_markup"] = normalized_markup
        response = self._call("sendMessage", payload)
        self._message_result(response, "sendMessage")
        return response

    def send_chat_message(self, chat_id: str, text: str) -> dict:
        """Send a regular bot message to an internal group or channel."""
        if chat_id is None or isinstance(chat_id, bool) or not str(chat_id).strip():
            raise ValueError("chat_id is required")
        response = self._call(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )
        self._message_result(response, "sendMessage")
        return response

    def edit_message_text(
        self,
        connection_id: str,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        preview_url: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = self._message_target(connection_id, chat_id, message_id)
        payload.update(
            text=text,
            link_preview_options=self._link_preview(preview_url),
        )
        if parse_mode:
            payload["parse_mode"] = parse_mode
        normalized_markup = normalize_inline_keyboard(reply_markup)
        if normalized_markup is not None:
            payload["reply_markup"] = normalized_markup
        try:
            response = self._call("editMessageText", payload)
        except TelegramAPIError as exc:
            # A retry after a lost response can legitimately find that the exact
            # text is already installed.  Treat Telegram's canonical response as
            # an idempotent success; every other edit error remains observable.
            if exc.status == 400 and "message is not modified" in str(exc).lower():
                return {
                    "ok": True,
                    "result": {"message_id": message_id},
                    "idempotent_replay": True,
                }
            raise
        self._message_result(response, "editMessageText")
        return response

    def edit_message_reply_markup(
        self,
        connection_id: str,
        chat_id: str,
        message_id: int,
        reply_markup: dict,
    ) -> dict:
        payload = self._message_target(connection_id, chat_id, message_id)
        normalized_markup = normalize_inline_keyboard(reply_markup)
        if normalized_markup is None:
            raise ValueError("inline reply_markup is required")
        payload["reply_markup"] = normalized_markup
        try:
            response = self._call("editMessageReplyMarkup", payload)
        except TelegramAPIError as exc:
            if exc.status == 400 and "message is not modified" in str(exc).lower():
                return {
                    "ok": True,
                    "result": {"message_id": message_id},
                    "idempotent_replay": True,
                }
            raise
        self._message_result(response, "editMessageReplyMarkup")
        return response

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
        cache_time: int = 0,
    ) -> dict:
        if (
            not isinstance(callback_query_id, str)
            or not callback_query_id
            or len(callback_query_id) > 256
            or any(ord(char) < 32 for char in callback_query_id)
        ):
            raise ValueError("callback_query_id is invalid")
        if text is not None and (
            not isinstance(text, str) or len(text) > 200 or "\x00" in text
        ):
            raise ValueError("callback answer text is invalid")
        if not isinstance(show_alert, bool):
            raise ValueError("show_alert must be boolean")
        if (
            isinstance(cache_time, bool)
            or not isinstance(cache_time, int)
            or not 0 <= cache_time <= 3600
        ):
            raise ValueError("cache_time is invalid")
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
            "cache_time": cache_time,
        }
        if text:
            payload["text"] = text
        response = self._call("answerCallbackQuery", payload)
        if response.get("result") is not True:
            # Repeating answerCallbackQuery is harmless and only clears the
            # client's spinner, so malformed success remains safely retryable.
            raise TelegramAPIError(
                "Telegram returned an invalid answerCallbackQuery result",
                status=200,
                retryable=True,
            )
        return response
