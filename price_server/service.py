from __future__ import annotations

import html
import json
import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .config import PriceSettings
from .sheets_registry import (
    ProductSortCalendarRegistry,
    ProductSortPostIndex,
    ProductSortPostRegistry,
)
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
        self._index_registry: ProductSortPostIndex | None = None
        self._last_index_hash = ""
        self._calendar_registry: ProductSortCalendarRegistry | None = None
        self._last_calendar_hash = ""

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
        record_key: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        section_key = str(_value(section, "section_key"))
        channel_id = str(message.chat_id)
        return {
            "record_key": (
                str(record_key).strip()
                or f"{channel_id}:{message.message_id}"
            ),
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
                record_key = ""
                if index < len(posts):
                    post = posts[index]
                    record_key = str(_value(post, "record_key", ""))
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
                        record_key=record_key,
                    )
                )

            for stale in posts[len(chunks):]:
                self.telegram.delete_message(
                    channel_id,
                    int(_value(stale, "message_id")),
                )
                self._supersede(stale, status="deleted")
            self.repository.retire_shared_telegram_post_aliases(
                section_key,
                channel_id,
                [int(_value(post, "message_id")) for post in posts],
            )
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

    @staticmethod
    def _preview_markup(job_id: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [[{
                "text": "❌ Отменить публикацию",
                "callback_data": f"price_cancel:{int(job_id)}",
            }]]
        }

    def create_scheduled_preview(self, job: Any) -> dict[str, Any]:
        """Publish a durable preview for one delayed job."""
        channel_id = self.settings.telegram_preview_channel_id
        if not channel_id:
            return {"status": "preview_disabled", "message_ids": []}
        job_id = int(_value(job, "job_id"))
        section = self._section_for_job(job)
        chunks = self._section_blocks(section)
        sent: list[TelegramMessage] = []
        try:
            for index, chunk in enumerate(chunks, start=1):
                message = self.telegram.send_message(
                    channel_id,
                    chunk,
                    reply_markup=(
                        self._preview_markup(job_id)
                        if index == 1 else None
                    ),
                    channel_username="",
                )
                sent.append(message)
                self.repository.record_scheduled_preview(
                    job_id,
                    str(_value(section, "section_key")),
                    channel_id,
                    part_no=index,
                    part_count=len(chunks),
                    message_id=message.message_id,
                    post_url=message.post_url,
                    content_hash=message.content_hash,
                    html_text=message.html,
                )
        except Exception as exc:
            if sent:
                try:
                    self.repository.set_scheduled_preview_status(
                        job_id, "active", error=str(exc)
                    )
                except Exception:
                    LOG.exception(
                        "price_partial_preview_persistence_failed job_id=%s",
                        job_id,
                    )
                raise PartialPublicationError(
                    "Telegram preview is partial and needs review"
                ) from exc
            raise
        return {
            "status": "previewed",
            "job_id": job_id,
            "message_ids": [message.message_id for message in sent],
        }

    def ensure_scheduled_previews(self, now: datetime) -> int:
        if not self.settings.preview_configured:
            return 0
        created = 0
        for job in self.repository.list_jobs_for_preview(
            now,
            horizon_hours=24,
            limit=100,
        ):
            try:
                self.create_scheduled_preview(job)
                created += 1
            except Exception:
                LOG.exception(
                    "price_scheduled_preview_failed job_id=%s",
                    _value(job, "job_id"),
                )
        return created

    def delete_scheduled_preview(
        self,
        job_id: int,
        *,
        status: str = "deleted",
    ) -> bool:
        previews = self.repository.list_scheduled_previews(
            job_id=int(job_id),
            status="active",
        )
        if not previews:
            return True
        try:
            for preview in previews:
                self.telegram.delete_message(
                    preview["channel_id"],
                    int(preview["message_id"]),
                )
        except Exception as exc:
            self.repository.set_scheduled_preview_status(
                int(job_id), "active", error=str(exc)
            )
            return False
        self.repository.set_scheduled_preview_status(int(job_id), status)
        return True

    def cancel_scheduled_job(self, job_id: int) -> bool:
        cancelled = self.repository.cancel_job(
            int(job_id),
            now=datetime.now(timezone.utc),
        )
        if cancelled:
            self.delete_scheduled_preview(int(job_id), status="cancelled")
        return bool(cancelled)

    def cleanup_terminal_previews(self) -> int:
        job_ids = list(dict.fromkeys(
            int(item["job_id"])
            for item in self.repository.list_terminal_previews(limit=500)
        ))
        cleaned = 0
        for job_id in job_ids:
            job = self.repository.get_job(job_id) or {}
            if self.delete_scheduled_preview(
                job_id,
                status=str(job.get("status") or "deleted"),
            ):
                cleaned += 1
        return cleaned

    def cleanup_superseded_posts(self, *, limit: int = 50) -> int:
        """Delete replaced channel posts without risking duplicate publishes."""
        now = datetime.now(timezone.utc)
        claimed = self.repository.claim_telegram_deletions(now, limit=limit)
        completed = 0
        for item in claimed:
            try:
                self.telegram.delete_message(
                    item["channel_id"],
                    int(item["message_id"]),
                )
            except Exception as exc:
                self.repository.retry_telegram_deletion(
                    item["deletion_id"],
                    datetime.now(timezone.utc),
                    exc,
                    permanent=not bool(getattr(exc, "retryable", True)),
                )
                LOG.warning(
                    "price_superseded_post_delete_failed deletion_id=%s type=%s",
                    item["deletion_id"],
                    type(exc).__name__,
                )
                continue
            if self.repository.complete_telegram_deletion(
                item["deletion_id"],
                datetime.now(timezone.utc),
            ):
                completed += 1
        return completed

    @staticmethod
    def _manual_deletion_markup(deletion_id: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [[{
                "text": "✅ Пост удалён",
                "callback_data": f"price_deleted:{int(deletion_id)}",
            }]]
        }

    def ensure_manual_deletion_requests(self, *, limit: int = 50) -> int:
        """Send one durable manual-cleanup link for each permanent failure."""
        channel_id = self.settings.telegram_preview_channel_id
        if not channel_id:
            return 0
        created = 0
        for item in self.repository.list_unreported_manual_deletions(limit=limit):
            deletion_id = int(item["deletion_id"])
            message_id = int(item["message_id"])
            post_url = str(item.get("post_url") or "").strip()
            if not post_url and self.settings.telegram_channel_username:
                post_url = (
                    f"https://t.me/{self.settings.telegram_channel_username}/"
                    f"{message_id}"
                )
            section_name = html.escape(
                str(item.get("section_name") or item["section_key"])
            )
            link = (
                f'<a href="{html.escape(post_url, quote=True)}">'
                f"Открыть старый пост #{message_id}</a>"
                if post_url else f"Старый post ID: <code>{message_id}</code>"
            )
            text = (
                "<b>🗑 Требуется ручное удаление</b>\n"
                f"Раздел: {section_name}\n"
                f"{link}\n\n"
                "Удалите старый пост, затем нажмите кнопку ниже."
            )
            request_message = self.telegram.send_message(
                channel_id,
                text,
                reply_markup=self._manual_deletion_markup(deletion_id),
                channel_username="",
            )
            try:
                self.repository.record_manual_deletion_request(
                    deletion_id,
                    request_channel_id=channel_id,
                    request_message_id=request_message.message_id,
                    target_post_url=post_url,
                )
            except Exception:
                try:
                    self.telegram.delete_message(
                        channel_id, request_message.message_id
                    )
                except Exception:
                    LOG.exception(
                        "price_manual_deletion_orphan_cleanup_failed deletion_id=%s",
                        deletion_id,
                    )
                raise
            created += 1
        return created

    @staticmethod
    def _is_service_channel_post(message: Mapping[str, Any]) -> bool:
        service_fields = {
            "pinned_message", "new_chat_title", "new_chat_photo",
            "delete_chat_photo", "group_chat_created",
            "supergroup_chat_created", "channel_chat_created",
            "message_auto_delete_timer_changed", "video_chat_scheduled",
            "video_chat_started", "video_chat_ended",
            "video_chat_participants_invited", "forum_topic_created",
            "forum_topic_closed", "forum_topic_reopened",
            "general_forum_topic_hidden", "general_forum_topic_unhidden",
            "write_access_allowed", "users_shared", "chat_shared",
        }
        return any(field in message for field in service_fields)

    def _handle_cancel_callback(self, callback: Mapping[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message")
        sender = callback.get("from")
        if not (
            callback_id and data.startswith("price_cancel:")
            and isinstance(message, Mapping)
            and isinstance(sender, Mapping)
        ):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or str(chat.get("id")) != str(
            self.settings.telegram_preview_channel_id
        ):
            self.telegram.answer_callback_query(
                callback_id, text="Недоступно", show_alert=True
            )
            return
        try:
            job_id = int(data.split(":", 1)[1])
            user_id = int(sender.get("id"))
        except (TypeError, ValueError):
            self.telegram.answer_callback_query(
                callback_id, text="Некорректная команда", show_alert=True
            )
            return
        member = self.telegram.get_chat_member(chat["id"], user_id)
        if str(member.get("status") or "") not in {"administrator", "creator"}:
            self.telegram.answer_callback_query(
                callback_id,
                text="Отмена доступна только администраторам",
                show_alert=True,
            )
            return
        cancelled = self.cancel_scheduled_job(job_id)
        self.telegram.answer_callback_query(
            callback_id,
            text=("Публикация отменена" if cancelled else "Задание уже обработано"),
            show_alert=not cancelled,
        )

    def _handle_manual_deleted_callback(
        self,
        callback: Mapping[str, Any],
    ) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message")
        sender = callback.get("from")
        if not (
            callback_id and data.startswith("price_deleted:")
            and isinstance(message, Mapping)
            and isinstance(sender, Mapping)
        ):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or str(chat.get("id")) != str(
            self.settings.telegram_preview_channel_id
        ):
            self.telegram.answer_callback_query(
                callback_id, text="Недоступно", show_alert=True
            )
            return
        try:
            deletion_id = int(data.split(":", 1)[1])
            user_id = int(sender.get("id"))
            request_message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            self.telegram.answer_callback_query(
                callback_id, text="Некорректная команда", show_alert=True
            )
            return
        member = self.telegram.get_chat_member(chat["id"], user_id)
        if str(member.get("status") or "") not in {"administrator", "creator"}:
            self.telegram.answer_callback_query(
                callback_id,
                text="Подтверждение доступно только администраторам",
                show_alert=True,
            )
            return
        request = self.repository.get_manual_deletion_request(deletion_id)
        if not request or str(request.get("status")) != "active" or (
            str(request.get("request_channel_id")) != str(chat["id"])
            or int(request.get("request_message_id") or 0) != request_message_id
        ):
            self.telegram.answer_callback_query(
                callback_id, text="Запрос уже обработан", show_alert=True
            )
            return
        try:
            # This also acts as verification: an already manually deleted post
            # is treated as success by TelegramClient, while an old post that
            # still exists returns "message can't be deleted".
            self.telegram.delete_message(
                request["target_channel_id"],
                int(request["target_message_id"]),
            )
        except Exception:
            self.telegram.answer_callback_query(
                callback_id,
                text="Сначала удалите старый пост, затем нажмите ещё раз",
                show_alert=True,
            )
            return
        try:
            self.telegram.delete_message(chat["id"], request_message_id)
        except Exception:
            self.telegram.answer_callback_query(
                callback_id,
                text="Не удалось удалить ссылку, попробуйте ещё раз",
                show_alert=True,
            )
            return
        completed = self.repository.complete_manual_deletion(
            deletion_id, datetime.now(timezone.utc)
        )
        self.telegram.answer_callback_query(
            callback_id,
            text=("Удаление отмечено" if completed else "Запрос уже обработан"),
            show_alert=not completed,
        )

    def poll_preview_updates(self) -> int:
        if not self.settings.preview_configured:
            return 0
        raw_offset = self.repository.get_runtime_state(
            "telegram_update_offset", "0"
        )
        try:
            offset = max(0, int(raw_offset))
        except ValueError:
            offset = 0
        updates = self.telegram.get_updates(
            offset=offset or None,
            limit=self.settings.telegram_updates_limit,
        )
        processed = 0
        for update in updates:
            update_id = int(update.get("update_id", -1))
            callback = update.get("callback_query")
            if isinstance(callback, Mapping):
                self._handle_cancel_callback(callback)
                self._handle_manual_deleted_callback(callback)
            for field in ("channel_post", "edited_channel_post"):
                message = update.get(field)
                if not isinstance(message, Mapping):
                    continue
                chat = message.get("chat")
                if (
                    isinstance(chat, Mapping)
                    and str(chat.get("id")) == str(
                        self.settings.telegram_preview_channel_id
                    )
                    and self._is_service_channel_post(message)
                    and message.get("message_id")
                ):
                    self.telegram.delete_message(
                        chat["id"], int(message["message_id"])
                    )
            if update_id >= 0:
                self.repository.set_runtime_state(
                    "telegram_update_offset", update_id + 1
                )
            processed += 1
        return processed

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

        completed = 0
        if supported:
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

            for item in supported:
                if self.repository.complete_outbox(
                    item["outbox_id"],
                    item["lease_token"],
                    datetime.now(timezone.utc),
                ):
                    completed += 1

        records = self.repository.build_post_index_records(
            self.settings.telegram_channel_id,
            self.settings.telegram_preview_channel_id,
        )
        stable = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        index_hash = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        if records and index_hash != self._last_index_hash:
            if self._index_registry is None:
                self._index_registry = ProductSortPostIndex(self.settings)
            self._index_registry.upsert(records)
            self._last_index_hash = index_hash
            completed += len(records)

        calendar_records = self.repository.list_calendar_plan()
        calendar_stable = json.dumps(
            calendar_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        calendar_hash = hashlib.sha256(
            calendar_stable.encode("utf-8")
        ).hexdigest()
        if calendar_records and calendar_hash != self._last_calendar_hash:
            if self._calendar_registry is None:
                self._calendar_registry = ProductSortCalendarRegistry(
                    self.settings
                )
            self._calendar_registry.replace(calendar_records)
            self._last_calendar_hash = calendar_hash
            completed += len(calendar_records)
        return completed
