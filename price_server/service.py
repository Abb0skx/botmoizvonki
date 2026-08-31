from __future__ import annotations

import html
import json
import hashlib
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import PriceSettings
from .quick_links import (
    CATALOG_QUICK_POST_KEY,
    QUICK_LINK_POST_SPECS,
)
from .post_formatting import format_price_sections
from .sheets_registry import (
    BotSettingsRegistry,
    ProductSortCalendarRegistry,
    ProductSortPostIndex,
    ProductSortPostRegistry,
    ProductSortQuickLinkRegistry,
    ProductSortQuickLinkRotationRegistry,
)
from .telegram_api import (
    TelegramClient,
    TelegramMessage,
)


LOG = logging.getLogger("price_server.service")
_EXCHANGE_RATE_COMMAND_RE = re.compile(r"^[0-9]{4,6}$")


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
        self._quick_link_registry: ProductSortQuickLinkRegistry | None = None
        self._last_quick_link_hash = ""
        self._quick_link_rotation_registry: (
            ProductSortQuickLinkRotationRegistry | None
        ) = None
        self._last_quick_link_rotation_hash = ""
        self._bot_settings_registry: BotSettingsRegistry | None = None
        self._quick_links_ready = False

    def _section_for_job(self, job: Any, section_key: str | None = None):
        section_key = str(
            section_key or _value(job, "section_key", "")
        ).strip()
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
    def _raw_section_blocks(section: Any) -> list[str]:
        raw = _value(section, "telegram_blocks")
        if raw is None:
            raw = _value(section, "telegram_blocks_json")
        if raw is None:
            payload = _value(section, "payload", {})
            if isinstance(payload, Mapping):
                raw = payload.get("telegram_blocks")
        return _json_list(raw)

    @classmethod
    def _section_blocks(cls, section: Any) -> list[str]:
        section_key = str(_value(section, "section_key", "")).strip()
        title = str(_value(section, "title", section_key)).strip()
        return format_price_sections([
            (section_key, title, cls._raw_section_blocks(section))
        ])

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

    @staticmethod
    def _bulk_section_keys(job: Any) -> list[str]:
        payload = _value(job, "payload", {})
        if not isinstance(payload, Mapping):
            return []
        raw = payload.get("section_keys")
        if not isinstance(raw, Sequence) or isinstance(
            raw, (str, bytes, bytearray)
        ):
            return []
        result = list(dict.fromkeys(
            str(item).strip() for item in raw if str(item).strip()
        ))
        return result[:100]

    def _edit_current_bulk(
        self,
        job: Any,
        channel_id: str,
    ) -> dict[str, Any]:
        payload = _value(job, "payload", {})
        if not isinstance(payload, Mapping):
            raise PublicationError("Bulk edit payload is invalid")
        section_keys = self._bulk_section_keys(job)
        if not section_keys:
            raise PublicationError("Bulk edit has no price sections")
        raw_expected = payload.get("expected_message_ids")
        if not isinstance(raw_expected, Sequence) or isinstance(
            raw_expected, (str, bytes, bytearray)
        ):
            raise PublicationError("Bulk edit has no expected message IDs")
        try:
            expected_ids = [int(item) for item in raw_expected]
        except (TypeError, ValueError) as exc:
            raise PublicationError("Bulk edit message IDs are invalid") from exc
        if (
            not expected_ids
            or any(item <= 0 for item in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
        ):
            raise PublicationError("Bulk edit message IDs are invalid")
        if len(expected_ids) != 1:
            return {
                "status": "skipped",
                "reason": "multi_part_binding_not_supported",
                "section_keys": section_keys,
                "message_ids": expected_ids,
            }

        sections = [
            self._section_for_job(job, section_key)
            for section_key in section_keys
        ]
        current_by_section = {
            section_key: self._current_posts(section_key, channel_id)
            for section_key in section_keys
        }
        raw_binding_fences = payload.get("expected_bindings")
        if not isinstance(raw_binding_fences, Mapping):
            return {
                "status": "skipped",
                "reason": "binding_fence_missing",
                "section_keys": section_keys,
                "message_ids": expected_ids,
            }

        fence_fields = (
            "record_key",
            "message_id",
            "part_no",
            "publication_id",
            "content_hash",
            "snapshot_id",
            "updated_at",
        )

        def binding_fence(item: Any) -> tuple[str, ...]:
            return tuple(str(_value(item, field, "")) for field in fence_fields)

        for section_key, current_posts in current_by_section.items():
            expected_posts = raw_binding_fences.get(section_key)
            if not isinstance(expected_posts, Sequence) or isinstance(
                expected_posts,
                (str, bytes, bytearray),
            ):
                return {
                    "status": "skipped",
                    "reason": "binding_fence_missing",
                    "section_keys": section_keys,
                    "message_ids": expected_ids,
                }
            if (
                [binding_fence(post) for post in current_posts]
                != [binding_fence(post) for post in expected_posts]
            ):
                return {
                    "status": "skipped",
                    "reason": "current_binding_changed",
                    "section_keys": section_keys,
                    "message_ids": expected_ids,
                }
        expected_tuple = tuple(expected_ids)
        if any(
            tuple(int(_value(post, "message_id")) for post in posts)
            != expected_tuple
            for posts in current_by_section.values()
        ):
            return {
                "status": "skipped",
                "reason": "current_binding_changed",
                "section_keys": section_keys,
                "message_ids": expected_ids,
            }

        chunks = format_price_sections([
            (
                str(_value(section, "section_key", "")),
                str(_value(section, "title", "")),
                self._raw_section_blocks(section),
            )
            for section in sections
        ])
        if len(chunks) != len(expected_ids):
            return {
                "status": "skipped",
                "reason": "message_shape_changed",
                "section_keys": section_keys,
                "message_ids": expected_ids,
                "expected_parts": len(expected_ids),
                "rendered_parts": len(chunks),
            }

        messages = [
            self.telegram.edit_message(channel_id, message_id, chunk)
            for message_id, chunk in zip(expected_ids, chunks, strict=True)
        ]
        registry_payloads: list[dict[str, Any]] = []
        for section in sections:
            section_key = str(_value(section, "section_key"))
            posts = current_by_section[section_key]
            for index, (post, message) in enumerate(
                zip(posts, messages, strict=True),
                start=1,
            ):
                registry_payloads.append(self._post_payload(
                    section=section,
                    message=message,
                    part_no=index,
                    part_count=len(messages),
                    publication_mode="edit",
                    is_current=True,
                    status="published",
                    sent_at=str(
                        _value(post, "sent_at", "")
                        or datetime.now(timezone.utc).isoformat()
                    ),
                    publication_id=str(
                        _value(post, "publication_id", "")
                        or uuid.uuid4().hex
                    ),
                    record_key=str(_value(post, "record_key", "")),
                ))
        self.repository.upsert_telegram_posts_atomic(registry_payloads)
        return {
            "status": "updated",
            "section_keys": section_keys,
            "message_ids": expected_ids,
        }

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

    def ensure_quick_link_registry(self) -> int:
        """Install approved index posts once and queue initial reconciliation."""
        if not self.settings.telegram_configured:
            return 0
        changed = self.repository.ensure_quick_link_posts(
            QUICK_LINK_POST_SPECS,
            channel_id=self.settings.telegram_channel_id,
            channel_username=self.settings.telegram_channel_username,
            enqueue_initial=True,
        )
        self._quick_links_ready = True
        return changed

    @staticmethod
    def _render_quick_link(
        post: Mapping[str, Any],
        *,
        quick_post_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        context_override: Mapping[str, Any] | None = None,
    ) -> tuple[str, list[dict]]:
        rendered = str(post.get("template_html") or "")
        resolved = [
            dict(item) for item in post.get("resolved_targets", [])
            if isinstance(item, Mapping)
        ]
        for target in resolved:
            link_key = str(target.get("link_key") or "")
            target_kind = str(
                target.get("target_kind") or "price_section"
            )
            if target_kind == "quick_post":
                target_key = str(
                    target.get("target_quick_post_key") or link_key
                )
                override = dict(
                    (quick_post_overrides or {}).get(target_key) or {}
                )
                if override:
                    target.update({
                        "target_channel_id": str(
                            override.get("target_channel_id")
                            or target.get("target_channel_id") or ""
                        ),
                        "target_message_id": int(
                            override.get("target_message_id")
                            or target.get("target_message_id") or 0
                        ),
                        "target_url": str(
                            override.get("target_url")
                            or target.get("target_url") or ""
                        ),
                    })
                marker = "{{quick_post_url:" + link_key + "}}"
            else:
                marker = "{{post_url:" + link_key + "}}"
            rendered = rendered.replace(
                marker,
                html.escape(str(target.get("target_url") or ""), quote=True),
            )
        context = dict(post.get("context") or {})
        context.update(dict(context_override or {}))
        for key, value in context.items():
            rendered = rendered.replace(
                "{{context:" + str(key) + "}}",
                html.escape(str(value)),
            )
        if any(marker in rendered for marker in (
            "{{post_url:", "{{quick_post_url:", "{{context:",
        )):
            raise PublicationError("Quick-link template has unresolved targets")
        return rendered, resolved

    def refresh_quick_link_posts(
        self,
        now_utc: datetime | str | None = None,
        *,
        limit: int = 20,
    ) -> int:
        """Edit quick-link indexes independently from price publication jobs."""
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        claimed = self.repository.claim_quick_link_updates(
            now,
            limit=limit,
            lease_seconds=180,
        )
        completed = 0
        for task in claimed:
            key = str(task["quick_post_key"])
            token = str(task["lease_token"])
            try:
                post = self.repository.resolve_quick_link_post(key)
                rendered, resolved = self._render_quick_link(post)
                render_hash = hashlib.sha256(
                    rendered.encode("utf-8")
                ).hexdigest()
                self.telegram.edit_message(
                    post["channel_id"],
                    int(post["message_id"]),
                    rendered,
                    channel_username=post["channel_username"],
                )
                if self.repository.complete_quick_link_update(
                    key,
                    token,
                    now,
                    rendered_html=rendered,
                    render_hash=render_hash,
                    resolved_targets=resolved,
                ):
                    completed += 1
            except Exception as exc:
                self.repository.retry_quick_link_update(
                    key,
                    token,
                    now,
                    exc,
                    retry_after_seconds=getattr(exc, "retry_after", None),
                    permanent=not bool(getattr(exc, "retryable", True)),
                )
                LOG.warning(
                    "price_quick_link_update_failed quick_post_key=%s type=%s",
                    key,
                    type(exc).__name__,
                )
        return completed

    def _rotation_render_plan(
        self,
        rotation: Mapping[str, Any],
    ) -> dict[str, Any]:
        main = self.repository.resolve_quick_link_post(
            CATALOG_QUICK_POST_KEY
        )
        secondary_key = str(rotation["secondary_quick_post_key"])
        secondary = self.repository.resolve_quick_link_post(secondary_key)
        previous_main_id = int(main["message_id"])
        previous_secondary_id = int(secondary["message_id"])
        rotation_date = date.fromisoformat(str(rotation["local_date"]))
        context = main.get("context")
        try:
            context_date = datetime.strptime(
                str(
                    context.get("catalog_date")
                    if isinstance(context, Mapping)
                    else ""
                ),
                "%d.%m.%Y",
            ).date()
        except ValueError:
            context_date = None
        display_date = max(
            rotation_date,
            context_date or rotation_date,
        ).strftime("%d.%m.%Y")
        send_main_html, _send_main_targets = self._render_quick_link(
            main,
            context_override={"catalog_date": display_date},
        )
        main_html, main_targets = self._render_quick_link(
            main,
            quick_post_overrides={
                secondary_key: {
                    "target_channel_id": str(main["channel_id"]),
                    "target_message_id": previous_main_id,
                    "target_url": str(main["post_url"]),
                }
            },
            context_override={"catalog_date": display_date},
        )
        secondary_html, secondary_targets = self._render_quick_link(
            secondary
        )
        return {
            "channel_id": str(main["channel_id"]),
            "channel_username": str(main["channel_username"]),
            "previous_main_message_id": previous_main_id,
            "previous_main_post_url": str(main["post_url"]),
            "previous_secondary_message_id": previous_secondary_id,
            "previous_secondary_post_url": str(secondary["post_url"]),
            "send_main_html": send_main_html,
            "main_html": main_html,
            "main_render_hash": hashlib.sha256(
                main_html.encode("utf-8")
            ).hexdigest(),
            "main_targets": main_targets,
            "secondary_html": secondary_html,
            "secondary_render_hash": hashlib.sha256(
                secondary_html.encode("utf-8")
            ).hexdigest(),
            "secondary_targets": secondary_targets,
        }

    def process_quick_link_rotations(
        self,
        now: datetime,
        *,
        limit: int = 1,
    ) -> int:
        """Advance durable catalogue rotations without repeating sendMessage."""
        completed = 0
        for _ in range(max(1, min(10, int(limit)))):
            rotation = self.repository.claim_due_quick_link_rotation(now)
            if rotation is None:
                break
            rotation_id = int(rotation["rotation_id"])
            lease_token = str(rotation["lease_token"])
            try:
                while True:
                    phase = str(rotation["phase"])
                    if phase == "planned":
                        plan = self._rotation_render_plan(rotation)
                        marked = (
                            self.repository.mark_quick_link_rotation_send_inflight(
                                rotation_id,
                                lease_token,
                                previous_main_message_id=plan[
                                    "previous_main_message_id"
                                ],
                                previous_main_post_url=plan[
                                    "previous_main_post_url"
                                ],
                                previous_secondary_message_id=plan[
                                    "previous_secondary_message_id"
                                ],
                                previous_secondary_post_url=plan[
                                    "previous_secondary_post_url"
                                ],
                                main_html=plan["main_html"],
                                main_render_hash=plan["main_render_hash"],
                                main_targets=plan["main_targets"],
                                secondary_html=plan["secondary_html"],
                                secondary_render_hash=plan[
                                    "secondary_render_hash"
                                ],
                                secondary_targets=plan["secondary_targets"],
                                now=now,
                            )
                        )
                        if not marked:
                            raise PublicationError(
                                "Quick-link rotation lease was lost"
                            )
                        sent = self.telegram.send_message(
                            plan["channel_id"],
                            plan["send_main_html"],
                            channel_username=plan["channel_username"],
                        )
                        try:
                            saved = (
                                self.repository.record_quick_link_rotation_main_sent(
                                    rotation_id,
                                    lease_token,
                                    message_id=sent.message_id,
                                    post_url=sent.post_url,
                                    now=now,
                                )
                            )
                        except Exception as exc:
                            raise PartialPublicationError(
                                "New catalogue was sent but its ID was not saved"
                            ) from exc
                        if not saved:
                            raise PartialPublicationError(
                                "New catalogue was sent after the lease was lost"
                            )
                    elif phase == "send_inflight":
                        raise PartialPublicationError(
                            "Catalogue send outcome requires manual review"
                        )
                    elif phase == "main_sent":
                        self.telegram.pin_message(
                            rotation["channel_id"]
                            if rotation.get("channel_id")
                            else self.settings.telegram_channel_id,
                            int(rotation["new_main_message_id"]),
                        )
                        if not self.repository.mark_quick_link_rotation_phase(
                            rotation_id,
                            lease_token,
                            expected_phase="main_sent",
                            phase="new_pinned",
                        ):
                            raise PublicationError(
                                "Quick-link pin state was not saved"
                            )
                    elif phase == "new_pinned":
                        self.telegram.edit_message(
                            self.settings.telegram_channel_id,
                            int(rotation["previous_main_message_id"]),
                            str(rotation["secondary_html"]),
                            channel_username=(
                                self.settings.telegram_channel_username
                            ),
                        )
                        if not self.repository.mark_quick_link_rotation_phase(
                            rotation_id,
                            lease_token,
                            expected_phase="new_pinned",
                            phase="secondary_edited",
                        ):
                            raise PublicationError(
                                "Recycled secondary edit was not saved"
                            )
                    elif phase == "secondary_edited":
                        self.telegram.edit_message(
                            self.settings.telegram_channel_id,
                            int(rotation["new_main_message_id"]),
                            str(rotation["main_html"]),
                            channel_username=(
                                self.settings.telegram_channel_username
                            ),
                        )
                        if not self.repository.mark_quick_link_rotation_phase(
                            rotation_id,
                            lease_token,
                            expected_phase="secondary_edited",
                            phase="catalog_edited",
                        ):
                            raise PublicationError(
                                "New catalogue link edit was not saved"
                            )
                    elif phase == "catalog_edited":
                        if not self.repository.commit_quick_link_rotation_swap(
                            rotation_id,
                            lease_token,
                        ):
                            raise PublicationError(
                                "Quick-link rotation swap was not saved"
                            )
                    elif phase == "swapped":
                        self.telegram.unpin_message(
                            self.settings.telegram_channel_id,
                            int(rotation["previous_main_message_id"]),
                        )
                        if not self.repository.mark_quick_link_rotation_phase(
                            rotation_id,
                            lease_token,
                            expected_phase="swapped",
                            phase="old_unpinned",
                        ):
                            raise PublicationError(
                                "Previous catalogue unpin was not saved"
                            )
                    elif phase == "old_unpinned":
                        if not self.repository.complete_quick_link_rotation(
                            rotation_id,
                            lease_token,
                        ):
                            raise PublicationError(
                                "Quick-link rotation completion was not saved"
                            )
                        completed += 1
                        break
                    else:
                        raise PublicationError(
                            f"Unsupported quick-link rotation phase: {phase}"
                        )
                    refreshed = self.repository.get_quick_link_rotation(
                        rotation_id
                    )
                    if refreshed is None:
                        raise PublicationError(
                            "Quick-link rotation disappeared"
                        )
                    rotation = refreshed
            except Exception as exc:
                self.repository.retry_quick_link_rotation(
                    rotation_id,
                    lease_token,
                    now,
                    exc,
                    retry_after_seconds=getattr(exc, "retry_after", None),
                    permanent=(
                        not bool(getattr(exc, "retryable", True))
                        and not bool(getattr(exc, "ambiguous", False))
                    ),
                    ambiguous=bool(getattr(exc, "ambiguous", False)),
                )
                LOG.warning(
                    "price_quick_link_rotation_failed rotation_id=%s type=%s",
                    rotation_id,
                    type(exc).__name__,
                )
                break
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

    def _cleanup_manual_deletion_request_message(
        self,
        request: Mapping[str, Any],
    ) -> bool:
        deletion_id = int(request["deletion_id"])
        try:
            self.telegram.delete_message(
                request["request_channel_id"],
                int(request["request_message_id"]),
            )
        except Exception as exc:
            LOG.warning(
                "price_manual_deletion_request_cleanup_failed "
                "deletion_id=%s type=%s",
                deletion_id,
                type(exc).__name__,
            )
            return False
        return self.repository.mark_manual_deletion_request_removed(
            deletion_id,
            request_channel_id=str(request["request_channel_id"]),
            request_message_id=int(request["request_message_id"]),
        )

    def cleanup_completed_manual_deletion_requests(
        self,
        *,
        limit: int = 50,
    ) -> int:
        """Idempotently remove helper links after SQLite records completion."""
        return sum(
            self._cleanup_manual_deletion_request_message(request)
            for request in self.repository.list_completed_manual_deletion_requests(
                limit=limit
            )
        )

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
        if not self._quick_links_ready:
            self.telegram.answer_callback_query(
                callback_id,
                text="Реестр быстрых ссылок ещё не готов",
                show_alert=True,
            )
            return
        request = self.repository.get_manual_deletion_request(deletion_id)
        if not request or (
            str(request.get("request_channel_id")) != str(chat["id"])
            or int(request.get("request_message_id") or 0) != request_message_id
        ):
            self.telegram.answer_callback_query(
                callback_id, text="Запрос уже обработан", show_alert=True
            )
            return
        request_status = str(request.get("status") or "")
        if request_status == "completed":
            removed = self._cleanup_manual_deletion_request_message(request)
            self.telegram.answer_callback_query(
                callback_id,
                text=(
                    "Служебная ссылка удалена"
                    if removed else "Удаление уже отмечено"
                ),
                show_alert=False,
            )
            return
        if request_status != "active":
            self.telegram.answer_callback_query(
                callback_id, text="Запрос уже обработан", show_alert=True
            )
            return
        if bool(request.get("quick_link_blocked")):
            self.telegram.answer_callback_query(
                callback_id,
                text="Сначала дождитесь обновления быстрой ссылки",
                show_alert=True,
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
        completed = self.repository.complete_manual_deletion(
            deletion_id, datetime.now(timezone.utc)
        )
        if completed:
            self._cleanup_manual_deletion_request_message(request)
        self.telegram.answer_callback_query(
            callback_id,
            text=("Удаление отмечено" if completed else "Запрос уже обработан"),
            show_alert=not completed,
        )

    def _record_exchange_rate_channel_post(
        self,
        update_id: int,
        message: Mapping[str, Any],
    ) -> bool:
        """Recognize a plain numeric rate only in the control channel."""

        chat = message.get("chat")
        if not isinstance(chat, Mapping) or str(chat.get("id")) != str(
            self.settings.telegram_preview_channel_id
        ):
            return False
        sender = message.get("from")
        if (
            isinstance(sender, Mapping)
            and bool(sender.get("is_bot"))
        ) or message.get("via_bot") is not None:
            return False
        raw_text = message.get("text")
        if not isinstance(raw_text, str):
            return False
        text = raw_text.strip()
        if _EXCHANGE_RATE_COMMAND_RE.fullmatch(text) is None:
            return False
        rate = int(text)
        if not 5000 <= rate <= 50000:
            return False
        try:
            message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            return False
        if message_id <= 0:
            return False
        self.repository.record_exchange_rate_request(
            source_update_id=int(update_id),
            source_channel_id=str(chat["id"]),
            source_message_id=message_id,
            rate=rate,
        )
        return True

    @staticmethod
    def _external_error_is_permanent(exc: Exception) -> bool:
        retryable = getattr(exc, "retryable", None)
        if retryable is not None:
            return not bool(retryable)
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        try:
            code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            code = None
        if code is not None:
            return code != 429 and code < 500
        return isinstance(exc, (TypeError, ValueError))

    def process_exchange_rate_updates(
        self,
        now_utc: datetime | str | None = None,
    ) -> int:
        """Advance one durable rate change without re-sending ambiguously."""

        if not (
            self.settings.telegram_configured
            and self.settings.preview_configured
        ):
            return 0
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        request = self.repository.claim_exchange_rate_request(now)
        if request is None:
            return 0
        request_id = int(request["request_id"])
        token = str(request["lease_token"])
        phase = str(request["phase"])
        try:
            if phase == "planned":
                if not self.repository.prepare_exchange_rate_catalog_update(
                    request_id,
                    token,
                    now,
                ):
                    raise PublicationError(
                        "Exchange-rate catalogue lease was lost"
                    )
                return 1

            if phase == "main_queued":
                outcome = self.repository.observe_exchange_rate_catalog_update(
                    request_id,
                    token,
                    now,
                )
                if outcome != "applied":
                    return 1
                phase = "main_applied"

            if phase == "main_applied":
                if self._bot_settings_registry is None:
                    self._bot_settings_registry = BotSettingsRegistry(
                        self.settings
                    )
                requested_at = datetime.fromisoformat(
                    str(request["requested_at"])
                )
                self._bot_settings_registry.update_exchange_rate(
                    int(request["rate"]),
                    requested_at,
                )
                if not self.repository.mark_exchange_rate_sheet_updated(
                    request_id,
                    token,
                    now,
                ):
                    raise PublicationError(
                        "Exchange-rate Sheet result could not be persisted"
                    )
                phase = "sheet_updated"

            if phase == "sheet_updated":
                if not self.repository.mark_exchange_rate_confirmation_inflight(
                    request_id,
                    token,
                    now,
                ):
                    raise PublicationError(
                        "Exchange-rate confirmation lease was lost"
                    )
                sent = self.telegram.send_message(
                    self.settings.telegram_preview_channel_id,
                    (
                        "✅ Курс изменён: 1 $ = "
                        f"{request['formatted_rate']} сум"
                    ),
                )
                if not self.repository.record_exchange_rate_confirmation_sent(
                    request_id,
                    token,
                    int(sent.message_id),
                    datetime.now(timezone.utc),
                ):
                    raise PartialPublicationError(
                        "Exchange-rate confirmation was sent but not persisted"
                    )
                phase = "confirmed"

            if phase == "confirmed":
                self.telegram.delete_message(
                    request["source_channel_id"],
                    int(request["source_message_id"]),
                )
                if not self.repository.complete_exchange_rate_request(
                    request_id,
                    token,
                    datetime.now(timezone.utc),
                ):
                    raise PublicationError(
                        "Exchange-rate completion lease was lost"
                    )
                return 1

            raise PublicationError(
                f"Unsupported exchange-rate phase: {phase}"
            )
        except Exception as exc:
            self.repository.retry_exchange_rate_request(
                request_id,
                token,
                datetime.now(timezone.utc),
                exc,
                retry_after_seconds=getattr(exc, "retry_after", None),
                # A fixed permission, worksheet, or schema problem is
                # recoverable without another Telegram edit because the
                # exact Sheet write is idempotent. Keep the numeric command
                # visible and retry after the operator fixes the Sheet.
                permanent=(
                    phase != "main_applied"
                    and self._external_error_is_permanent(exc)
                ),
                ambiguous=bool(getattr(exc, "ambiguous", False)),
            )
            LOG.warning(
                "price_exchange_rate_update_failed request_id=%s phase=%s type=%s",
                request_id,
                phase,
                type(exc).__name__,
            )
            return 0

    def poll_preview_updates(self) -> int:
        if not self.settings.telegram_configured:
            return 0
        service_channels = {
            str(channel_id)
            for channel_id in (
                self.settings.telegram_channel_id,
                self.settings.telegram_preview_channel_id,
            )
            if str(channel_id).strip()
        }
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
            channel_post = update.get("channel_post")
            if (
                update_id >= 0
                and isinstance(channel_post, Mapping)
            ):
                self._record_exchange_rate_channel_post(
                    update_id,
                    channel_post,
                )
            for field in ("channel_post", "edited_channel_post"):
                message = update.get(field)
                if not isinstance(message, Mapping):
                    continue
                chat = message.get("chat")
                if (
                    isinstance(chat, Mapping)
                    and str(chat.get("id")) in service_channels
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
        channel_id = str(
            _value(job, "channel_id", "")
            or self.settings.telegram_channel_id
        ).strip()
        if not channel_id:
            raise PublicationError("Telegram channel is not configured")
        action = str(_value(job, "action", "")).strip().casefold()
        payload = _value(job, "payload", {})
        if (
            action in {"edit", "edit_current"}
            and isinstance(payload, Mapping)
            and payload.get("source") == "price_admin_edit_all"
        ):
            return self._edit_current_bulk(job, channel_id)
        section = self._section_for_job(job)
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

        quick_link_records = self.repository.build_quick_link_registry_records()
        quick_link_stable = json.dumps(
            quick_link_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        quick_link_hash = hashlib.sha256(
            quick_link_stable.encode("utf-8")
        ).hexdigest()
        if (
            quick_link_records
            and quick_link_hash != self._last_quick_link_hash
        ):
            if self._quick_link_registry is None:
                self._quick_link_registry = ProductSortQuickLinkRegistry(
                    self.settings
                )
            self._quick_link_registry.upsert(quick_link_records)
            self._last_quick_link_hash = quick_link_hash
            completed += len(quick_link_records)

        rotation_records = self.repository.list_quick_link_rotations(
            limit=1000
        )
        rotation_stable = json.dumps(
            rotation_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rotation_hash = hashlib.sha256(
            rotation_stable.encode("utf-8")
        ).hexdigest()
        if (
            rotation_records
            and rotation_hash != self._last_quick_link_rotation_hash
        ):
            if self._quick_link_rotation_registry is None:
                self._quick_link_rotation_registry = (
                    ProductSortQuickLinkRotationRegistry(self.settings)
                )
            self._quick_link_rotation_registry.upsert(rotation_records)
            self._last_quick_link_rotation_hash = rotation_hash
            completed += len(rotation_records)
        return completed
