"""Automate control SMS; never dial an MMI code through MyCalls."""
import hashlib
import json
from dataclasses import replace
from html import escape

import requests

from .config import DEVICES, ROUTES, RouteConfig
from .repository import utc_timestamp
from .service import ForwardingService, dial_digit_signature, event_timestamp
from .device_controls import DeviceSMSControls, COMMANDS


SMS_DEVICES = {key: replace(value, controls_enabled=True) for key, value in DEVICES.items()}
SMS_ROUTES = dict(ROUTES)
for destination in ("redmi", "tecno"):
    SMS_ROUTES[("poco", destination)] = RouteConfig(
        "poco", destination, SMS_DEVICES[destination].sim_number
    )


class SMSForwardingService(ForwardingService):
    devices = SMS_DEVICES

    def __init__(self, *, send_sms, **kwargs):
        self.send_sms = send_sms
        telegram_api = kwargs["telegram_api"]

        def safe_telegram_api(*args, **options):
            try:
                return telegram_api(*args, **options)
            except requests.RequestException as exc:
                # HTTPError.__str__ contains Telegram's token-bearing URL.
                # Preserve the API description for already-unpinned handling,
                # but never persist the URL in logs or forwarding_control_posts.
                description = "Telegram: " + type(exc).__name__
                if exc.response is not None:
                    try:
                        description = str(exc.response.json().get("description") or description)
                    except (ValueError, AttributeError):
                        pass
                raise RuntimeError(description[:500]) from None

        kwargs["telegram_api"] = safe_telegram_api
        super().__init__(make_call=self._send_command, **kwargs)
        with self.repository.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS forwarding_sms_replies (
                event_key TEXT PRIMARY KEY, operation_id INTEGER,
                received_at INTEGER NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL
            )""")
        self.device_controls = DeviceSMSControls(self)
        self.repository.control_guard = self.device_controls.guard

    @staticmethod
    def service_number(source_code, target_code):
        if not isinstance(source_code, str) or not isinstance(target_code, str):
            raise ValueError("Неверная команда")
        if source_code not in SMS_DEVICES:
            raise ValueError("Неизвестный телефон")
        if target_code == "off":
            return "disable", "OFF", None, None
        if (source_code, target_code) not in SMS_ROUTES:
            raise ValueError("Такой маршрут не настроен")
        target = SMS_DEVICES[target_code]
        return "enable", "ON " + target.name, target, target.sim_number

    def _send_command(self, user_login, command):
        source = next((d for d in self.devices.values() if d.moizvonki_user == user_login), None)
        allowed = {"OFF"} | {
            "ON " + self.devices[target].name
            for source_code, target in SMS_ROUTES if source and source_code == source.code
        }
        # Reject legacy queued MMI commands rather than dispatching after upgrade.
        if not source or command not in allowed:
            raise ValueError("Устаревшая или неверная команда; отправка отменена")
        response = self.send_sms(source.sim_number, self.devices["poco"].moizvonki_user, command)
        return {"body": response, "http_status": 200}

    @staticmethod
    def build_keyboard():
        rows = []
        for source in SMS_DEVICES.values():
            rows.append([
                {"text": source.name + " → " + SMS_DEVICES[target].name,
                 "callback_data": f"fwd:{source.code}:{target}"}
                for origin, target in SMS_ROUTES if origin == source.code
            ])
            rows.append([{"text": source.name + ": отменить", "callback_data": f"fwd:{source.code}:off"}])
        return {"inline_keyboard": rows}

    def _status_text(self, device):
        status = device.get("operation_status")
        labels = {
            "queued": "⏳ SMS в очереди", "sending": "⏳ Отправляем SMS через Poco",
            "api_accepted": "⏳ SMS принято сервисом; ждём ответ телефона",
            "sms_reply_received": "📩 Получен ответ телефона — проверьте текст оператора",
            "unconfirmed": "⚠️ Подтверждения нет. Автоматического повтора не будет",
            "api_failed": "❌ Ошибка отправки команды",
        }
        result = labels.get(status, "— Нет актуального подтверждения по SMS")
        stamp = self._format_time(device.get("operation_request_time"))
        return result + (" · " + stamp if stamp else "")

    def states(self):
        result = self.repository.list_device_states()
        for device in result:
            device["status_label"] = self._status_text(device)
            device["reply"] = device.get("operation_result") if device.get("operation_status") == "sms_reply_received" else None
            device["controls"] = [{"key": key, "label": value[1], "command": value[0]}
                                  for key, value in COMMANDS.items()
                                  if device["code"] == "poco" or not key.startswith("hotspot_")]
            device["control_state"] = self.device_controls.state(device["code"])
        return result

    def run_once(self, now_ts=None):
        now_ts = int(now_ts or utc_timestamp())
        result = super().run_once(now_ts)
        if self.settings.enabled:
            result["device_control"] = self.device_controls.dispatch_one(now_ts)
        return result

    def build_post_text(self):
        lines = ["<b>📞 Переадресация · управление по SMS</b>",
                 "Команды отправляются с Poco. Нажатие изменяет переадресацию выбранного телефона.", ""]
        if not self.settings.enabled:
            lines.append("⛔ Управление временно отключено.")
        for device in self.states():
            lines.extend([f"<b>{escape(device['name'])}</b> · {escape(device['sim_number'])}", device["status_label"]])
            if device["reply"]:
                lines.append("<blockquote>" + escape(device["reply"][:650]) + "</blockquote>")
            lines.append("")
        lines.extend(["Принятие SMS не подтверждает включение переадресации. Ответ оператора появится при получении ответного SMS на Poco.",
                      "Пауза между командами: 5 минут. Poco → Poco требует доставки SMS самому себе.",
                      "Redmi: свой оператор; Tecno: свой оператор; Abbos: все телефоны."])
        return "\n".join(lines)

    def queue_web(self, source, target, request_id):
        if not self.settings.enabled:
            raise ValueError("Управление отключено")
        action, command, destination, number = self.service_number(source, target)
        return self.repository.queue_operation(
            callback_query_id="web:" + request_id, employee=self.devices[source],
            action=action, target=destination, target_number=number, service_number=command,
            requested_by=0, requested_username="dashboard-admin", telegram_chat_id=self.chat_id,
            telegram_message_id=0, now_ts=utc_timestamp(),
            cooldown_seconds=self.settings.command_cooldown_seconds,
            correlation_window_seconds=self.settings.correlation_window_seconds,
        )

    def _dial_matches(self, operation, actual, event, now_ts):
        if operation["service_number"] == "OFF" or operation["service_number"].startswith("ON "):
            return False
        return super()._dial_matches(operation, actual, event, now_ts)

    def handle_sms_reply(self, webhook, event, now_ts=None):
        now_ts = int(now_ts or utc_timestamp())
        if str(event.get("direction")) != "0" or str(event.get("event_type")) != "32":
            return False
        if str(webhook.get("user_login", "")).strip().casefold() != self.devices["poco"].moizvonki_user:
            return False
        sender = dial_digit_signature(event.get("client_number"))
        source = next((d for d in self.devices.values() if dial_digit_signature(d.sim_number) == sender), None)
        text = str(event.get("text") or "").strip()[:10000]
        # Do not mistake ordinary employee SMS or commands for an Automate reply.
        if not source or not text:
            return False
        timestamp = event_timestamp(event, now_ts)
        key = hashlib.sha256(json.dumps([webhook.get("user_login"), event.get("db_call_id"), sender,
                                        event.get("start_time"), text], ensure_ascii=False).encode()).hexdigest()
        if self.device_controls.handle_reply(source.code, text, timestamp, now_ts, key):
            return True
        if "Батарея:" not in text or "Режим звонка:" not in text:
            return False
        with self.repository.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM forwarding_sms_replies WHERE event_key=?", (key,)).fetchone():
                return True
            # No command ID is returned by Automate. Require exactly one recent
            # dispatched operation; ambiguous/delayed replies are never attributed.
            rows = conn.execute("""SELECT * FROM forwarding_operations
                WHERE employee_id=? AND attempt_count > 0
                AND service_number IN ('OFF','ON Redmi','ON Tecno','ON Poco')
                AND request_time <= ? AND request_time >= ?
                ORDER BY id DESC""", (source.code, timestamp, timestamp - self.settings.correlation_window_seconds)).fetchall()
            row = rows[0] if len(rows) == 1 else None
            first_line = text.splitlines()[0].strip()
            known_commands = {value[0] for value in COMMANDS.values()} | {"OFF", "ON Poco", "ON Redmi", "ON Tecno"}
            if row and first_line in known_commands and first_line != row["service_number"]:
                row = None
            conn.execute("INSERT INTO forwarding_sms_replies VALUES (?,?,?,?,?)",
                         (key, row["id"] if row else None, now_ts, sender, text))
            if row and row["status"] in {"sending", "api_accepted", "unconfirmed"}:
                conn.execute("""UPDATE forwarding_operations SET status='sms_reply_received',
                    completed_at=?, result=?, error=NULL WHERE id=?""", (now_ts, text, row["id"]))
                conn.execute("""UPDATE forwarding_devices SET forwarding_status='unknown', updated_at=?
                    WHERE code=? AND last_operation_id=?""", (now_ts, source.code, row["id"]))
        self._safe_refresh()
        return True
