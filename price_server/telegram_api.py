"""Telegram Bot API client for price publications.

The price service is outbound-only, so it does not need polling or a webhook.
This module deliberately keeps Telegram-specific transport and message-size
rules away from the repository and the web router.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence

import requests


LOG = logging.getLogger("price_server.telegram")

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CHUNK_TARGET = 3600
_PUBLIC_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


class TelegramAPIError(RuntimeError):
    """A normalized Telegram/transport failure used by the scheduler."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.retryable = retryable
        # A send timeout may mean Telegram accepted the message but its response
        # never reached us. Retrying such an operation can create a duplicate.
        self.ambiguous = ambiguous


class TelegramContentTooLong(ValueError):
    """A semantic block cannot be split without damaging its HTML entities."""


class TelegramBatchError(TelegramAPIError):
    """A multi-message operation failed after zero or more successful sends."""

    def __init__(
        self,
        cause: TelegramAPIError,
        sent_messages: Sequence["TelegramMessage"],
    ) -> None:
        super().__init__(
            str(cause),
            error_code=cause.error_code,
            retry_after=cause.retry_after,
            retryable=cause.retryable,
            ambiguous=cause.ambiguous,
        )
        self.cause = cause
        self.sent_messages = tuple(sent_messages)


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    chat_id: str
    post_url: str
    html: str
    content_hash: str


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def telegram_visible_text(html_text: str) -> str:
    """Return the text Telegram counts after parsing HTML entities."""

    parser = _VisibleTextParser()
    parser.feed(str(html_text))
    parser.close()
    return parser.text


def telegram_text_units(html_text: str) -> int:
    """Conservatively count Telegram text using UTF-16 code units.

    Telegram entity offsets use UTF-16. Counting astral emoji as two units is
    slightly conservative for the message limit and avoids boundary failures.
    """

    visible = telegram_visible_text(html_text)
    return len(visible.encode("utf-16-le")) // 2


def split_telegram_blocks(
    telegram_blocks: Sequence[str],
    *,
    target_units: int = TELEGRAM_CHUNK_TARGET,
    max_units: int = TELEGRAM_MESSAGE_LIMIT,
    separator: str = "\n\n",
) -> list[str]:
    """Pack complete semantic HTML blocks into Telegram-sized messages.

    A block normally represents one complete model. We never cut inside a
    block because doing so could break an ``<a>`` or ``<b>`` entity. The
    renderer must therefore make each supplied block independently valid HTML.
    """

    if not (1 <= target_units <= max_units <= TELEGRAM_MESSAGE_LIMIT):
        raise ValueError("invalid Telegram chunk limits")

    blocks = [
        str(block).strip()
        for block in telegram_blocks
        if str(block).strip()
    ]
    if not blocks:
        raise ValueError("telegram_blocks must contain at least one block")

    separator_units = telegram_text_units(separator)
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0

    for index, block in enumerate(blocks):
        block_units = telegram_text_units(block)
        if block_units > max_units:
            raise TelegramContentTooLong(
                "Telegram block "
                f"{index} is {block_units} UTF-16 units; maximum is {max_units}"
            )

        added_units = block_units + (separator_units if current else 0)
        # Prefer the lower target, but always allow a legal block to use the
        # remaining room up to Telegram's hard maximum.
        if current and current_units + added_units > target_units:
            chunks.append(separator.join(current))
            current = []
            current_units = 0
            added_units = block_units

        if current and current_units + added_units > max_units:
            chunks.append(separator.join(current))
            current = []
            current_units = 0
            added_units = block_units

        current.append(block)
        current_units += added_units

    if current:
        chunks.append(separator.join(current))

    if any(telegram_text_units(chunk) > max_units for chunk in chunks):
        raise AssertionError("Telegram chunker produced an oversized message")
    return chunks


def build_post_url(
    chat_id: str | int,
    message_id: int,
    channel_username: str = "",
) -> str:
    """Build a public or private-channel Telegram post URL."""

    if isinstance(message_id, bool) or int(message_id) <= 0:
        raise ValueError("message_id must be positive")

    username = str(channel_username).strip().lstrip("@")
    raw_chat_id = str(chat_id).strip()
    if not username and raw_chat_id.startswith("@"):
        username = raw_chat_id[1:]

    if username:
        if not _PUBLIC_USERNAME.fullmatch(username):
            raise ValueError("invalid Telegram channel username")
        return f"https://t.me/{username}/{int(message_id)}"

    if raw_chat_id.startswith("-100") and raw_chat_id[4:].isdigit():
        return f"https://t.me/c/{raw_chat_id[4:]}/{int(message_id)}"

    # A numeric peer id without the -100 prefix cannot be converted reliably
    # to a t.me/c URL. The message is still usable through chat_id/message_id.
    return ""


class TelegramClient:
    """Small synchronous Bot API client used from the scheduler worker."""

    def __init__(
        self,
        token: str,
        *,
        channel_username: str = "",
        session: requests.Session | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 45.0,
    ) -> None:
        token = str(token).strip()
        if not token:
            raise ValueError("Telegram bot token is required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self.channel_username = str(channel_username).strip().lstrip("@")
        self.session = session or requests.Session()
        self.timeout = (float(connect_timeout), float(read_timeout))

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _request(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        ambiguous_on_transport_error: bool = False,
    ) -> Any:
        try:
            response = self.session.post(
                f"{self._base_url}/{method}",
                json=dict(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TelegramAPIError(
                f"Telegram {method} transport error: {type(exc).__name__}",
                retryable=not ambiguous_on_transport_error,
                ambiguous=ambiguous_on_transport_error,
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            ambiguous = bool(
                ambiguous_on_transport_error
                and (
                    response.status_code >= 500
                    or 200 <= response.status_code < 300
                )
            )
            raise TelegramAPIError(
                f"Telegram {method} returned invalid JSON (HTTP {response.status_code})",
                error_code=response.status_code,
                retryable=response.status_code >= 500 and not ambiguous,
                ambiguous=ambiguous,
            ) from exc

        if response.ok and isinstance(body, Mapping) and body.get("ok"):
            return body.get("result")

        description = str(
            body.get("description", "Telegram API request failed")
            if isinstance(body, Mapping)
            else "Telegram API request failed"
        )
        error_code = (
            int(body.get("error_code"))
            if isinstance(body, Mapping) and body.get("error_code") is not None
            else response.status_code
        )
        parameters = body.get("parameters", {}) if isinstance(body, Mapping) else {}
        retry_after_raw = (
            parameters.get("retry_after") if isinstance(parameters, Mapping) else None
        )
        try:
            retry_after = (
                max(0.0, float(retry_after_raw))
                if retry_after_raw is not None
                else None
            )
        except (TypeError, ValueError):
            retry_after = None

        if method == "editMessageText" and "message is not modified" in description.casefold():
            return True
        if (
            method == "pinChatMessage"
            and "already pinned" in description.casefold()
        ):
            return True
        if method == "unpinChatMessage" and any(
            phrase in description.casefold()
            for phrase in ("not pinned", "message to unpin not found")
        ):
            return True
        if method == "deleteMessage" and "message to delete not found" in description.casefold():
            # Deletion is idempotent for a leased edit job. Telegram may have
            # succeeded before the server saved the result.
            return True
        if (
            method == "deleteMessage"
            and "message to delete not found" in description.casefold()
        ):
            # Deletion is a desired-state operation. A retry after an uncertain
            # response may legitimately find that the first request succeeded.
            return True

        retryable = bool(error_code == 429 or error_code >= 500)
        ambiguous = bool(ambiguous_on_transport_error and error_code >= 500)
        raise TelegramAPIError(
            f"Telegram {method} failed ({error_code}): {description}",
            error_code=error_code,
            retry_after=retry_after,
            retryable=retryable and not ambiguous,
            ambiguous=ambiguous,
        )

    def send_message(
        self,
        chat_id: str | int,
        html_text: str,
        *,
        disable_web_page_preview: bool = True,
        reply_markup: Mapping[str, Any] | None = None,
        channel_username: str | None = None,
    ) -> TelegramMessage:
        if telegram_text_units(html_text) > TELEGRAM_MESSAGE_LIMIT:
            raise TelegramContentTooLong("Telegram message exceeds 4096 units")

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self._request(
            "sendMessage",
            payload,
            ambiguous_on_transport_error=True,
        )
        if not isinstance(result, Mapping) or not result.get("message_id"):
            raise TelegramAPIError(
                "Telegram sendMessage response has no message_id",
                ambiguous=True,
            )
        message_id = int(result["message_id"])
        result_chat = result.get("chat", {})
        result_chat_id = (
            str(result_chat.get("id"))
            if isinstance(result_chat, Mapping) and result_chat.get("id") is not None
            else str(chat_id)
        )
        return TelegramMessage(
            message_id=message_id,
            chat_id=result_chat_id,
            post_url=build_post_url(
                result_chat_id,
                message_id,
                self.channel_username
                if channel_username is None
                else channel_username,
            ),
            html=html_text,
            content_hash=self._hash(html_text),
        )

    def send_blocks(
        self,
        chat_id: str | int,
        telegram_blocks: Sequence[str],
    ) -> list[TelegramMessage]:
        chunks = split_telegram_blocks(telegram_blocks)
        sent: list[TelegramMessage] = []
        try:
            for chunk in chunks:
                sent.append(self.send_message(chat_id, chunk))
        except TelegramAPIError as exc:
            raise TelegramBatchError(exc, sent) from exc
        return sent

    def edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        html_text: str,
        *,
        disable_web_page_preview: bool = True,
        channel_username: str | None = None,
    ) -> TelegramMessage:
        if telegram_text_units(html_text) > TELEGRAM_MESSAGE_LIMIT:
            raise TelegramContentTooLong("Telegram message exceeds 4096 units")
        self._request(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            },
        )
        return TelegramMessage(
            message_id=int(message_id),
            chat_id=str(chat_id),
            post_url=build_post_url(
                chat_id,
                int(message_id),
                self.channel_username
                if channel_username is None
                else channel_username,
            ),
            html=html_text,
            content_hash=self._hash(html_text),
        )

    def delete_message(self, chat_id: str | int, message_id: int) -> bool:
        result = self._request(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )
        return bool(result)

    def pin_message(
        self,
        chat_id: str | int,
        message_id: int,
        *,
        disable_notification: bool = True,
    ) -> bool:
        result = self._request(
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "disable_notification": bool(disable_notification),
            },
        )
        return bool(result)

    def unpin_message(
        self,
        chat_id: str | int,
        message_id: int,
    ) -> bool:
        result = self._request(
            "unpinChatMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )
        return bool(result)

    def get_updates(
        self,
        *,
        offset: int | None = None,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        payload: dict[str, Any] = {
            "limit": min(100, max(1, int(limit))),
            "timeout": 0,
            "allowed_updates": [
                "callback_query",
                "channel_post",
                "edited_channel_post",
            ],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._request("getUpdates", payload)
        if not isinstance(result, Sequence) or isinstance(
            result, (str, bytes, bytearray)
        ):
            raise TelegramAPIError("Telegram getUpdates response is not a list")
        return [item for item in result if isinstance(item, Mapping)]

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {
            "callback_query_id": str(callback_query_id),
            "show_alert": bool(show_alert),
        }
        if text:
            payload["text"] = str(text)[:200]
        return bool(self._request("answerCallbackQuery", payload))

    def get_chat_member(
        self,
        chat_id: str | int,
        user_id: int,
    ) -> Mapping[str, Any]:
        result = self._request(
            "getChatMember",
            {"chat_id": chat_id, "user_id": int(user_id)},
        )
        if not isinstance(result, Mapping):
            raise TelegramAPIError(
                "Telegram getChatMember response is not an object"
            )
        return result
