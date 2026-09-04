import json
import re
import threading

from datetime import datetime, timezone
from html import escape
from urllib.parse import unquote

import requests

from .config import (
    DEVICES,
    OPERATOR,
    ROUTES,
    DeviceConfig,
    ForwardingSettings,
)
from .repository import (
    CORRELATABLE_OPERATION_STATUSES,
    ForwardingRepository,
    utc_timestamp,
)


CALLBACK_PREFIX = "fwd:"


def canonical_dial_string(value: str | None) -> str:
    """Preserve carrier-control characters and remove presentation spaces."""
    return "".join(
        char
        for char in unquote(str(value or ""))
        if char in "+*#0123456789"
    )


def dial_digit_signature(value: str | None) -> str:
    return "".join(
        char for char in str(value or "") if char.isdigit()
    )


def service_dial_matches(expected: str, actual: str) -> bool:
    """Match an exact MMI string or the phone-number form made by dialers.

    Some Android dialers collapse ``**21*+number#`` into ``+21number``.
    Recognising only the complete digit signature keeps that failed service
    attempt out of customer statistics and automatic SMS, without treating
    the plain forwarding target as a service command.
    """
    expected_canonical = canonical_dial_string(expected)
    actual_canonical = canonical_dial_string(actual)
    if not expected_canonical or not actual_canonical:
        return False
    if actual_canonical == expected_canonical:
        return True
    if "*" in actual_canonical or "#" in actual_canonical:
        return False
    return dial_digit_signature(actual_canonical) == dial_digit_signature(
        expected_canonical
    )


def event_timestamp(event: dict, fallback: int) -> int:
    for key in ("start_time", "event_created"):
        try:
            value = int(event.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return int(fallback)


class ForwardingService:
    def __init__(
        self,
        *,
        repository: ForwardingRepository,
        settings: ForwardingSettings,
        chat_id,
        telegram_api,
        make_call,
        local_timezone=timezone.utc,
    ):
        self.repository = repository
        self.settings = settings
        self.chat_id = chat_id
        self.telegram_api = telegram_api
        self.make_call = make_call
        self.local_timezone = local_timezone
        self._refresh_lock = threading.Lock()

    @staticmethod
    def service_number(
        source_code: str,
        target_code: str | None,
    ) -> tuple[str, str, DeviceConfig | None, str | None]:
        employee = DEVICES.get(source_code)
        if not employee or not employee.controls_enabled:
            raise ValueError("Управление для этого телефона отключено")

        if target_code == "off":
            return "disable", OPERATOR.disable_number, None, None

        route = ROUTES.get((source_code, str(target_code or "")))
        if not route:
            raise ValueError("Такой маршрут переадресации не настроен")

        target = DEVICES[route.target_code]
        service_number = OPERATOR.enable_template.format(
            target=route.target_number
        )
        return "enable", service_number, target, route.target_number

    @staticmethod
    def build_keyboard() -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "Redmi: 📞 Poco",
                        "callback_data": "fwd:redmi:poco",
                    },
                    {
                        "text": "Redmi: 📞 Tecno",
                        "callback_data": "fwd:redmi:tecno",
                    },
                ],
                [
                    {
                        "text": "Redmi: ❌ Отмена",
                        "callback_data": "fwd:redmi:off",
                    }
                ],
                [
                    {
                        "text": "Tecno: 📞 Poco",
                        "callback_data": "fwd:tecno:poco",
                    },
                    {
                        "text": "Tecno: 📞 Redmi",
                        "callback_data": "fwd:tecno:redmi",
                    },
                ],
                [
                    {
                        "text": "Tecno: ❌ Отмена",
                        "callback_data": "fwd:tecno:off",
                    }
                ],
            ]
        }

    def _format_time(self, timestamp: int | None) -> str:
        if not timestamp:
            return ""
        return datetime.fromtimestamp(
            int(timestamp),
            self.local_timezone,
        ).strftime("%d.%m %H:%M")

    def _status_text(self, device: dict) -> str:
        if not device["controls_enabled"]:
            return "ℹ️ Кнопки пока отключены"

        status = device.get("operation_status")
        target_code = (
            device.get("operation_target_code")
            or device.get("forwarding_target_code")
        )
        target_name = (
            DEVICES[target_code].name
            if target_code in DEVICES
            else ""
        )
        event_time = (
            device.get("operation_completed_at")
            or device.get("operation_request_time")
        )
        time_label = self._format_time(event_time)
        suffix = f" · {time_label}" if time_label else ""

        if status in {"queued", "sending"}:
            return "⏳ Команда отправляется" + suffix
        if status == "api_accepted":
            return "⏳ API принял команду, ждём телефон" + suffix
        if status == "call_started":
            return "📲 Телефон начал служебный звонок" + suffix
        if status in {"call_completed", "external_call_completed"}:
            action = device.get("operation_action")
            if action == "disable":
                result = "✅ Служебный звонок отмены завершён"
            else:
                result = "✅ Служебный звонок завершён"
                if target_name:
                    result += f" → {target_name}"
            return result + suffix
        if status in {
            "call_not_completed",
            "external_call_not_completed",
        }:
            return "❌ Звонок не состоялся или был отменён" + suffix
        if status == "call_wrong_sim":
            return "⚠️ Звонок выполнен через другую SIM" + suffix
        if status == "call_completed_sim_unverified":
            return "⚠️ Звонок завершён, но SIM не подтверждена" + suffix
        if status == "api_failed":
            return "❌ МоиЗвонки отклонил команду" + suffix
        if status == "unconfirmed":
            return "⚠️ Нет подтверждения от телефона" + suffix
        if status == "external_call_started":
            return "📲 Обнаружен служебный звонок" + suffix

        forwarding_status = device.get("forwarding_status")
        if forwarding_status == "enabled_unverified" and target_name:
            return f"✅ Последний звонок: переадресация → {target_name}*"
        if forwarding_status == "disabled_unverified":
            return "✅ Последний звонок: отмена переадресации*"
        return "— Команд ещё не было"

    def build_post_text(self) -> str:
        states = self.repository.list_device_states()
        lines = [
            "<b>📞 Управление переадресацией</b>",
            "",
            "Команда выполняется только после нажатия кнопки.",
            "",
        ]
        for device in states:
            lines.extend(
                [
                    (
                        f"<b>📱 {escape(device['name'])}</b> "
                        f"<code>{escape(device['sim_number'])}</code>"
                    ),
                    self._status_text(device),
                    "",
                ]
            )
        lines.extend(
            [
                "<i>* МоиЗвонки подтверждает служебный звонок, "
                "но не возвращает отдельный статус оператора.</i>",
            ]
        )
        return "\n".join(lines)

    def queue_callback(
        self,
        *,
        callback_query_id: str,
        callback_data: str,
        telegram_user: dict,
        chat_id,
        message_id,
        now_ts: int | None = None,
    ) -> dict:
        now_ts = int(now_ts or utc_timestamp())

        if not self.settings.enabled:
            return {
                "queued": False,
                "reason": "disabled",
                "message": "Управление переадресацией временно отключено",
                "show_alert": True,
            }

        if (
            not isinstance(callback_query_id, str)
            or not callback_query_id.strip()
        ):
            return {
                "queued": False,
                "reason": "invalid",
                "message": "Неверный идентификатор команды",
                "show_alert": True,
            }

        raw_actor_id = telegram_user.get("id")
        actor_id = (
            raw_actor_id
            if type(raw_actor_id) is int and raw_actor_id > 0
            else 0
        )

        parts = str(callback_data or "").split(":")
        if len(parts) != 3 or parts[0] != "fwd":
            return {
                "queued": False,
                "reason": "invalid",
                "message": "Неверная команда",
                "show_alert": True,
            }

        source_code, target_code = parts[1], parts[2]
        employee = DEVICES.get(source_code)
        if not employee or not employee.controls_enabled:
            return {
                "queued": False,
                "reason": "invalid",
                "message": "Управление для этого телефона отключено",
                "show_alert": True,
            }

        if not self.settings.can_control(actor_id, source_code):
            return {
                "queued": False,
                "reason": "forbidden",
                "message": (
                    "У вас нет доступа к управлению "
                    f"{employee.name}"
                ),
                "show_alert": True,
            }

        if str(chat_id) != str(self.chat_id):
            return {
                "queued": False,
                "reason": "wrong_chat",
                "message": "Эта кнопка работает только в канале звонков",
                "show_alert": True,
            }

        try:
            numeric_message_id = int(message_id)
        except (TypeError, ValueError):
            numeric_message_id = 0
        if not self.repository.is_current_post(
            chat_id,
            numeric_message_id,
        ):
            return {
                "queued": False,
                "reason": "stale_post",
                "message": "Это старый пульт. Используйте закреплённый пост",
                "show_alert": True,
            }

        try:
            action, service_number, target, target_number = (
                self.service_number(source_code, target_code)
            )
        except ValueError as exc:
            return {
                "queued": False,
                "reason": "invalid",
                "message": str(exc),
                "show_alert": True,
            }

        username = (
            telegram_user.get("username")
            or telegram_user.get("first_name")
            or telegram_user.get("last_name")
            or ""
        )
        result = self.repository.queue_operation(
            callback_query_id=callback_query_id,
            employee=employee,
            action=action,
            target=target,
            target_number=target_number,
            service_number=service_number,
            requested_by=actor_id,
            requested_username=str(username),
            telegram_chat_id=chat_id,
            telegram_message_id=numeric_message_id,
            now_ts=now_ts,
            cooldown_seconds=self.settings.command_cooldown_seconds,
            correlation_window_seconds=(
                self.settings.correlation_window_seconds
            ),
        )
        reason = result["reason"]
        if result["queued"]:
            result.update(
                {
                    "message": "⏳ Команда принята. Ждём ответ телефона",
                    "show_alert": False,
                }
            )
        elif reason == "busy":
            result.update(
                {
                    "message": "Для этого телефона команда уже выполняется",
                    "show_alert": True,
                }
            )
        elif reason == "cooldown":
            result.update(
                {
                    "message": (
                        "Защита от повтора: подождите "
                        f"{result.get('retry_after', 1)} сек."
                    ),
                    "show_alert": True,
                }
            )
        elif reason == "unconfirmed":
            result.update(
                {
                    "message": (
                        "Предыдущий звонок ещё может прийти с задержкой. "
                        "Повтор будет доступен через "
                        f"{result.get('retry_after', 1)} сек."
                    ),
                    "show_alert": True,
                }
            )
        else:
            result.update(
                {
                    "message": "Эта команда уже была принята",
                    "show_alert": False,
                }
            )
        return result

    def _telegram_message_id(self, result: dict) -> int:
        message = result.get("result") if isinstance(result, dict) else None
        if not isinstance(message, dict) or not message.get("message_id"):
            raise RuntimeError("Telegram не вернул message_id")
        return int(message["message_id"])

    def ensure_daily_post(self, now_ts: int | None = None) -> dict:
        now_ts = int(now_ts or utc_timestamp())
        local_now = datetime.fromtimestamp(now_ts, self.local_timezone)
        if (local_now.hour, local_now.minute) < (
            self.settings.post_hour,
            self.settings.post_minute,
        ):
            return {"created": False, "reason": "not_due"}
        if not self.chat_id:
            return {"created": False, "reason": "missing_chat_id"}

        local_date = local_now.date().isoformat()
        reservation = self.repository.reserve_daily_post(
            local_date,
            str(self.chat_id),
            now_ts,
        )
        post = reservation["post"]
        created = False

        if reservation["claimed"]:
            try:
                result = self.telegram_api(
                    "sendMessage",
                    data={
                        "chat_id": self.chat_id,
                        "text": self.build_post_text(),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": True,
                        "reply_markup": json.dumps(
                            self.build_keyboard(),
                            ensure_ascii=False,
                        ),
                    },
                    timeout=30,
                )
                message_id = self._telegram_message_id(result)
                self.repository.mark_post_sent(
                    local_date,
                    message_id,
                    now_ts,
                )
                post = self.repository.get_daily_post(local_date)
                created = True
            except Exception as exc:
                self.repository.mark_post_error(
                    local_date,
                    repr(exc),
                    now_ts,
                    release_reservation=False,
                )
                raise

        if not post or post.get("message_id") is None:
            return {"created": created, "reason": "reserved"}

        if (
            post.get("last_error")
            and int(post.get("lease_until") or 0) > now_ts
        ):
            return {
                "created": created,
                "reason": "retry_wait",
                "post": post,
            }

        if post.get("pinned_at") is None:
            try:
                self.telegram_api(
                    "pinChatMessage",
                    data={
                        "chat_id": self.chat_id,
                        "message_id": int(post["message_id"]),
                        "disable_notification": True,
                    },
                    timeout=30,
                )
                self.repository.mark_post_pinned(local_date, now_ts)
                post = self.repository.get_daily_post(local_date)
            except Exception as exc:
                if "already pinned" not in str(exc).casefold():
                    self.repository.mark_post_error(
                        local_date,
                        repr(exc),
                        now_ts,
                    )
                    raise
                self.repository.mark_post_pinned(local_date, now_ts)
                post = self.repository.get_daily_post(local_date)

        if post.get("previous_unpinned_at") is None:
            previous = self.repository.previous_pinned_post(local_date)
            previous_message_id = (
                int(previous["message_id"]) if previous else None
            )
            previous_chat_id = (
                previous["chat_id"] if previous else None
            )
            previous_local_date = (
                previous["local_date"] if previous else None
            )
            if previous_message_id is not None:
                try:
                    self.telegram_api(
                        "unpinChatMessage",
                        data={
                            "chat_id": previous_chat_id,
                            "message_id": previous_message_id,
                        },
                        timeout=30,
                    )
                except Exception as exc:
                    lowered = str(exc).casefold()
                    harmless = (
                        "message to unpin not found" in lowered
                        or "message is not pinned" in lowered
                    )
                    if not harmless:
                        self.repository.mark_post_error(
                            local_date,
                            repr(exc),
                            now_ts,
                        )
                        raise
            self.repository.mark_post_active(
                local_date,
                previous_local_date,
                previous_message_id,
                now_ts,
            )

        return {
            "created": created,
            "reason": "active",
            "post": self.repository.get_daily_post(local_date),
        }

    def refresh_current_post(self) -> bool:
        with self._refresh_lock:
            post = self.repository.get_current_post()
            if not post:
                return False
            self.telegram_api(
                "editMessageText",
                data={
                    "chat_id": post["chat_id"],
                    "message_id": int(post["message_id"]),
                    "text": self.build_post_text(),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": json.dumps(
                        self.build_keyboard(),
                        ensure_ascii=False,
                    ),
                },
                timeout=30,
            )
            return True

    def _safe_refresh(self) -> None:
        try:
            self.refresh_current_post()
        except Exception as exc:
            print("FORWARDING POST UPDATE ERROR:", repr(exc))

    def dispatch_one(self, now_ts: int | None = None) -> dict:
        now_ts = int(now_ts or utc_timestamp())
        operation = self.repository.claim_next_operation(now_ts)
        if not operation:
            return {"processed": False}

        try:
            api_result = self.make_call(
                operation["moizvonki_user"],
                operation["service_number"],
            )
            self.repository.mark_api_accepted(
                operation["id"],
                now_ts=now_ts,
                http_status=api_result.get("http_status"),
                response=api_result.get("body"),
            )
            status = "api_accepted"
            print(
                "FORWARDING COMMAND ACCEPTED:",
                operation["id"],
                operation["employee_name"],
                operation["service_number"],
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            self.repository.mark_dispatch_error(
                operation["id"],
                now_ts=now_ts,
                error=repr(exc),
                ambiguous=True,
            )
            status = "unconfirmed"
            print(
                "FORWARDING COMMAND UNKNOWN:",
                operation["id"],
                type(exc).__name__,
            )
        except Exception as exc:
            self.repository.mark_dispatch_error(
                operation["id"],
                now_ts=now_ts,
                error=repr(exc),
                ambiguous=False,
            )
            status = "api_failed"
            print(
                "FORWARDING COMMAND ERROR:",
                operation["id"],
                repr(exc),
            )

        current_operation = self.repository.get_operation(operation["id"])
        if current_operation:
            status = current_operation["status"]
        self._safe_refresh()
        return {
            "processed": True,
            "operation_id": operation["id"],
            "status": status,
        }

    def run_once(self, now_ts: int | None = None) -> dict:
        now_ts = int(now_ts or utc_timestamp())
        try:
            post_result = self.ensure_daily_post(now_ts)
        except Exception as exc:
            post_result = {
                "created": False,
                "reason": "telegram_error",
                "error": repr(exc),
            }
            print("FORWARDING DAILY POST ERROR:", repr(exc))
        expired = self.repository.expire_operations(
            now_ts,
            self.settings.confirmation_timeout_seconds,
        )
        if expired:
            self._safe_refresh()
        dispatch_result = self.dispatch_one(now_ts)
        return {
            "post": post_result,
            "expired": expired,
            "dispatch": dispatch_result,
        }

    def _dial_matches(
        self,
        operation: dict,
        actual: str,
        event: dict,
        now_ts: int,
    ) -> bool:
        expected = canonical_dial_string(operation["service_number"])
        actual_canonical = canonical_dial_string(actual)
        if not actual_canonical:
            return False

        event_time = event_timestamp(event, now_ts)
        request_time = int(
            operation.get("api_requested_at")
            or operation.get("request_time")
            or now_ts
        )
        fallback_is_timely = (
            request_time - 30
            <= event_time
            <= request_time
            + self.settings.confirmation_timeout_seconds
            + 60
        )
        if not fallback_is_timely:
            return False
        if service_dial_matches(expected, actual_canonical):
            return True
        if operation["action"] == "enable":
            return dial_digit_signature(actual_canonical) == (
                dial_digit_signature(operation.get("target_number"))
            )
        return False

    @staticmethod
    def _decode_exact_command(
        source_code: str,
        actual: str,
    ):
        actual_canonical = canonical_dial_string(actual)
        if service_dial_matches(OPERATOR.disable_number, actual_canonical):
            return "disable", None, None, OPERATOR.disable_number
        for (route_source, route_target), route in ROUTES.items():
            if route_source != source_code:
                continue
            service_number = OPERATOR.enable_template.format(
                target=route.target_number
            )
            if service_dial_matches(service_number, actual_canonical):
                return (
                    "enable",
                    DEVICES[route_target],
                    route.target_number,
                    service_number,
                )
            # Suppress delayed retries produced by the legacy command that
            # omitted Beeline's voice-service class (``*11``).
            legacy_service_number = f"**21*{route.target_number}#"
            if service_dial_matches(
                legacy_service_number,
                actual_canonical,
            ):
                return (
                    "enable",
                    DEVICES[route_target],
                    route.target_number,
                    legacy_service_number,
                )
        return None

    def is_known_service_event(
        self,
        webhook: dict,
        event: dict,
    ) -> bool:
        try:
            direction = int(event.get("direction") or 0)
        except (TypeError, ValueError):
            return False
        if direction != 1:
            return False
        user_login = str(webhook.get("user_login") or "").strip().casefold()
        employee = next(
            (
                device
                for device in DEVICES.values()
                if device.moizvonki_user.casefold() == user_login
            ),
            None,
        )
        return bool(
            employee
            and self._decode_exact_command(
                employee.code,
                str(event.get("client_number") or ""),
            )
        )

    def handle_provider_event(
        self,
        action: str,
        webhook: dict,
        event: dict,
        *,
        now_ts: int | None = None,
    ) -> dict:
        if action not in {"call.start", "call.answer", "call.finish"}:
            return {"handled": False}
        try:
            direction = int(event.get("direction") or 0)
        except (TypeError, ValueError):
            direction = 0
        if direction != 1:
            return {"handled": False}

        user_login = str(webhook.get("user_login") or "").strip().casefold()
        employee = next(
            (
                device
                for device in DEVICES.values()
                if device.moizvonki_user.casefold() == user_login
            ),
            None,
        )
        if not employee:
            return {"handled": False}

        actual = str(event.get("client_number") or "")
        now_ts = int(now_ts or utc_timestamp())

        # Webhooks may be retried after an operation has already reached a
        # terminal state. Provider identities remain authoritative even when
        # Android reports only the destination number instead of the original
        # star/hash service string.
        known_operation = (
            self.repository.operation_by_provider_identity(
                user_login,
                event
            )
        )
        if known_operation:
            if known_operation["status"] in (
                CORRELATABLE_OPERATION_STATUSES
            ):
                result = self.repository.mark_provider_event(
                    known_operation["id"],
                    action,
                    event,
                    now_ts,
                )
                self._safe_refresh()
                return {"handled": True, **result}
            return {
                "handled": True,
                "matched": True,
                "duplicate": True,
                "terminal": action == "call.finish",
                "operation": known_operation,
            }

        operations = self.repository.correlatable_operations(
            user_login,
            event_timestamp(event, now_ts),
            self.settings.correlation_window_seconds,
        )
        for operation in operations:
            if self._dial_matches(
                operation,
                actual,
                event,
                now_ts,
            ):
                result = self.repository.mark_provider_event(
                    operation["id"],
                    action,
                    event,
                    now_ts,
                )
                self._safe_refresh()
                print(
                    "FORWARDING EVENT:",
                    action,
                    operation["id"],
                    result.get("operation", {}).get("status"),
                )
                return {"handled": True, **result}

        decoded = self._decode_exact_command(employee.code, actual)
        if not decoded:
            return {"handled": False}

        # A directly dialled service command must still stay out of customer
        # statistics and automatic SMS. Only call.finish is persisted here;
        # start/answer events are deliberately just suppressed.
        if action != "call.finish":
            return {"handled": True, "matched": False, "external": True}

        command_action, target, target_number, service_number = decoded
        result = self.repository.record_external_event(
            employee=employee,
            action=command_action,
            target=target,
            target_number=target_number,
            service_number=service_number,
            event_action=action,
            event=event,
            now_ts=now_ts,
        )
        self._safe_refresh()
        return {"handled": True, "external": True, **result}


def validate_forwarding_config() -> None:
    command_pattern = re.compile(
        r"(?:##21#|\*\*21\*\+?[0-9]{7,15}\*11#)\Z"
    )
    for source_code, device in DEVICES.items():
        if device.controls_enabled:
            cancel = canonical_dial_string(OPERATOR.disable_number)
            if not command_pattern.fullmatch(cancel):
                raise RuntimeError("Некорректная команда отмены")
        for (route_source, _), route in ROUTES.items():
            if route_source != source_code:
                continue
            command = canonical_dial_string(
                OPERATOR.enable_template.format(target=route.target_number)
            )
            if not command_pattern.fullmatch(command):
                raise RuntimeError(
                    f"Некорректная команда маршрута {route_source}"
                )


validate_forwarding_config()
