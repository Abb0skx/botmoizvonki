from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .config import PriceSettings
from .sheets_registry import ProductSortPostRegistry
from .telegram_api import (
    TelegramClient,
    TelegramMessage,
    split_telegram_blocks,
)


LOG = logging.getLogger("price_server.service")


class PublicationError(RuntimeError):
    retryable = False
    retry_after = None
    ambiguous = False


class PartialPublicationError(PublicationError):
    """At least one new Telegram message exists; never retry blindly."""

    ambiguous = True


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return getattr(record, key, default)


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [value]
    else:
        decoded = value
    if not isinstance(decoded, Sequence) or isinstance(
        decoded, (str, bytes, bytearray)
    ):
        raise PublicationError("Section has no Telegram blocks")
    blocks = [str(item).strip() for item in decoded if str(item).strip()]
    if not blocks:
        raise PublicationError("Section has no Telegram blocks")
    return blocks


class PricePublicationService:
    def __init__(
        self,
        settings: PriceSettings,
        repository,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.telegram = telegram or TelegramClient(
            settings.telegram_bot_token,
            channel_username=settings.telegram_channel_username,
        )
        self._registry: ProductSortPostRegistry | None = None

    def _section_for_job(self, job: Any):
        section_key = str(_value(job, "section_key", "")).strip()
        snapshot_policy = str(
            _value(job, "snapshot_policy", "latest")
        ).strip()
        snapshot_id = (
            str(_value(job, "snapshot_id", "")).strip()
            if snapshot_policy == "pinned"
            else None
        )
        section = self.repository.get_section(
            section_key,
            snapshot_id=snapshot_id or None,
        )
        if section is None:
            raise PublicationError(
                f"Price section is not available: {section_key}"
            )
        return section

    @staticmethod
    def _section_blocks(section: Any) -> list[str]:
        raw = _value(section, "telegram_blocks")
        if raw is None:
            raw = _value(section, "telegram_blocks_json")
        if raw is None:
            payload = _value(section, "payload", {})
            if isinstance(payload, Mapping):
                raw = payload.get("telegram_blocks")
        return split_telegram_blocks(_json_list(raw))

    def _current_posts(self, section_key: str, channel_id: str) -> list[Any]:
        posts = self.repository.list_telegram_posts(
            section_key=section_key,
            channel_id=channel_id,
            current_only=True,
            limit=100,
        )
        return sorted(
            posts,
            key=lambda item: int(_value(item, "part_no", 0)),
        )

    def _post_payload(
        self,
        *,
        section: Any,
        message: TelegramMessage,
        part_no: int,
        part_count: int,
        publication_mode: str,
        is_current: bool,
        status: str,
        sent_at: str,
        publication_id: str,
        last_error: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        section_key = str(_value(section, "section_key"))
        channel_id = str(message.chat_id)
        return {
            "record_key": f"{channel_id}:{message.message_id}",
            "publication_id": publication_id,
            "section_key": section_key,
            "section_name": str(_value(section, "title", section_key)),
            "channel_id": channel_id,
            "channel_username": self.settings.telegram_channel_username,
            "part_no": int(part_no),
            "part_count": int(part_count),
            "message_id": int(message.message_id),
            "post_url": message.post_url,
            "snapshot_id": str(_value(section, "snapshot_id", "")),
            "content_hash": message.content_hash,
            "publication_mode": publication_mode,
            "is_current": bool(is_current),
            "status": status,
            "sent_at": sent_at,
            "updated_at": now,
            "last_error": str(last_error)[:1000],
            "html_text": message.html,
        }

    def _upsert(self, payload: Mapping[str, Any]) -> None:
        self.repository.upsert_telegram_post(dict(payload))

    def _supersede(self, post: Any, *, status: str = "superseded") -> None:
        payload = dict(post) if isinstance(post, Mapping) else {
            key: _value(post, key)
            for key in (
                "record_key", "publication_id", "section_key", "section_name",
                "channel_id", "channel_username", "part_no", "part_count",
                "message_id", "post_url", "snapshot_id", "content_hash",
                "publication_mode", "sent_at", "html_text",
            )
        }
        payload.update(
            {
                "is_current": False,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._upsert(payload)

    def _record_sent(
        self,
        section: Any,
        messages: list[TelegramMessage],
        *,
        publication_id: str,
        mode: str,
        current: bool,
        status: str,
        sent_at: str,
        error: str = "",
    ) -> None:
        for index, message in enumerate(messages, start=1):
            self._upsert(
                self._post_payload(
                    section=section,
                    message=message,
                    part_no=index,
                    part_count=len(messages),
                    publication_mode=mode,
                    is_current=current,
                    status=status,
                    sent_at=sent_at,
                    publication_id=publication_id,
                    last_error=error,
                )
            )

    def _send_new(self, section: Any, channel_id: str) -> dict[str, Any]:
        chunks = self._section_blocks(section)
        publication_id = uuid.uuid4().hex
        sent_at = datetime.now(timezone.utc).isoformat()
        sent: list[TelegramMessage] = []

        try:
            for chunk in chunks:
                message = self.telegram.send_message(channel_id, chunk)
                sent.append(message)
                self._upsert(
                    self._post_payload(
                        section=section,
                        message=message,
                        part_no=len(sent),
                        part_count=len(chunks),
                        publication_mode="send",
                        is_current=False,
                        status="publishing",
                        sent_at=sent_at,
                        publication_id=publication_id,
                    )
                )
        except Exception as exc:
            if sent:
                try:
                    self._record_sent(
                        section,
                        sent,
                        publication_id=publication_id,
                        mode="send",
                        current=False,
                        status="partial_error",
                        sent_at=sent_at,
                        error=str(exc),
                    )
                except Exception:
                    LOG.exception("price_partial_publication_persistence_failed")
                raise PartialPublicationError(
                    "Telegram publication is partial and needs review"
                ) from exc
            raise

        replacement = [
            self._post_payload(
                section=section,
                message=message,
                part_no=index,
                part_count=len(sent),
                publication_mode="send",
                is_current=True,
                status="published",
                sent_at=sent_at,
                publication_id=publication_id,
            )
            for index, message in enumerate(sent, start=1)
        ]
        try:
            self.repository.replace_current_telegram_posts(
                str(_value(section, "section_key")),
                channel_id,
                replacement,
            )
        except Exception as exc:
            # All Telegram sends already happened. Retrying could duplicate
            # them even when the persistence failure is temporary.
            raise PartialPublicationError(
                "Telegram publication was sent but not finalized"
            ) from exc
        return {
            "publication_id": publication_id,
            "message_ids": [message.message_id for message in sent],
            "post_urls": [message.post_url for message in sent],
        }

    def _edit_current(self, section: Any, channel_id: str) -> dict[str, Any]:
        section_key = str(_value(section, "section_key"))
        posts = self._current_posts(section_key, channel_id)
        if not posts:
            raise PublicationError(
                "There is no current Telegram post for this section"
            )

        chunks = self._section_blocks(section)
        publication_id = str(
            _value(posts[0], "publication_id", "") or uuid.uuid4().hex
        )
        sent_at = str(
            _value(posts[0], "sent_at", "")
            or datetime.now(timezone.utc).isoformat()
        )
        messages: list[TelegramMessage] = []
        sent_new_part = False

        try:
            for index, chunk in enumerate(chunks):
                if index < len(posts):
                    post = posts[index]
                    message = self.telegram.edit_message(
                        channel_id,
                        int(_value(post, "message_id")),
                        chunk,
                    )
                else:
                    message = self.telegram.send_message(channel_id, chunk)
                    sent_new_part = True
                messages.append(message)
                self._upsert(
                    self._post_payload(
                        section=section,
                        message=message,
                        part_no=index + 1,
                        part_count=len(chunks),
                        publication_mode="edit",
                        is_current=True,
                        status="published",
                        sent_at=sent_at,
                        publication_id=publication_id,
                    )
                )

            for stale in posts[len(chunks):]:
                self.telegram.delete_message(
                    channel_id,
                    int(_value(stale, "message_id")),
                )
                self._supersede(stale, status="deleted")
        except Exception as exc:
            if sent_new_part:
                raise PartialPublicationError(
                    "Telegram edit added a part that needs review"
                ) from exc
            raise

        return {
            "publication_id": publication_id,
            "message_ids": [message.message_id for message in messages],
            "post_urls": [message.post_url for message in messages],
        }

    def execute_job(self, job: Any) -> dict[str, Any]:
        section = self._section_for_job(job)
        channel_id = str(
            _value(job, "channel_id", "")
            or self.settings.telegram_channel_id
        ).strip()
        if not channel_id:
            raise PublicationError("Telegram channel is not configured")
        action = str(_value(job, "action", "")).strip().casefold()
        if action in {"send", "publish_new"}:
            return self._send_new(section, channel_id)
        if action in {"edit", "edit_current"}:
            return self._edit_current(section, channel_id)
        raise PublicationError(f"Unsupported publication action: {action}")

    def sync_sheets_outbox(self, *, limit: int = 50) -> int:
        """Mirror durable Telegram IDs to Product Sort without blocking posts."""

        now = datetime.now(timezone.utc)
        claimed = self.repository.claim_outbox(
            now,
            limit=limit,
            lease_seconds=180,
        )
        if not claimed:
            return 0

        supported = [
            item
            for item in claimed
            if item.get("entity_type") == "telegram_post"
            and item.get("operation") == "upsert"
            and isinstance(item.get("payload"), Mapping)
        ]
        unsupported = [item for item in claimed if item not in supported]
        for item in unsupported:
            self.repository.retry_outbox(
                item["outbox_id"],
                item["lease_token"],
                now,
                "unsupported Product Sort outbox item",
                permanent=True,
            )

        if not supported:
            return 0
        if self._registry is None:
            self._registry = ProductSortPostRegistry(self.settings)

        try:
            self._registry.upsert([item["payload"] for item in supported])
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            for item in supported:
                self.repository.retry_outbox(
                    item["outbox_id"],
                    item["lease_token"],
                    now,
                    str(exc),
                    retry_after_seconds=retry_after,
                )
            raise

        completed = 0
        for item in supported:
            if self.repository.complete_outbox(
                item["outbox_id"],
                item["lease_token"],
                datetime.now(timezone.utc),
            ):
                completed += 1
        return completed
