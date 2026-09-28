"""Queued Automate settings commands, separate from forwarding history."""
import json

import requests

COMMANDS = {
    "wifi_on": ("WIFI ON", "Wi-Fi: включить"),
    "wifi_off": ("WIFI OFF", "Wi-Fi: выключить"),
    "internet_on": ("INTERNET ON", "Мобильный интернет: включить"),
    "internet_off": ("INTERNET OFF", "Мобильный интернет: выключить"),
    "sound_on": ("SOUND ON", "Звук: включить (≈75%)"),
    "sound_off": ("SOUND OFF", "Беззвучный режим"),
    "location": ("?", "Запросить координаты"),
    "hotspot_on": ("HOTSPOT ON", "Точка доступа: включить"),
    "hotspot_off": ("HOTSPOT OFF", "Точка доступа: выключить"),
}


class DeviceSMSControls:
    def __init__(self, service):
        self.service = service
        self.connect = service.repository.connect
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS device_sms_commands (
                id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                device_code TEXT NOT NULL, command TEXT NOT NULL,
                requested_at INTEGER NOT NULL, status TEXT NOT NULL,
                dispatched_at INTEGER, completed_at INTEGER,
                response TEXT, error TEXT, reply TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_device_sms_device_time ON device_sms_commands(device_code,requested_at)")

    def guard(self, conn, device, now):
        row = conn.execute("SELECT * FROM device_sms_commands WHERE device_code=? ORDER BY id DESC LIMIT 1",
                           (device,)).fetchone()
        if not row:
            return None
        if row["status"] in {"queued", "sending", "api_accepted"}:
            return {"queued": False, "reason": "busy"}
        retry = row["requested_at"] + self.service.settings.command_cooldown_seconds - now
        if retry > 0:
            return {"queued": False, "reason": "cooldown", "retry_after": retry}
        return None

    def queue(self, device, key, request_id, now):
        if not self.service.settings.enabled:
            raise ValueError("Управление отключено")
        if not isinstance(device, str) or device not in self.service.devices:
            raise ValueError("Неизвестный телефон")
        if not isinstance(key, str) or key not in COMMANDS:
            raise ValueError("Неверная команда")
        if key.startswith("hotspot_") and device != "poco":
            raise ValueError("Точка доступа настроена только для Poco")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM device_sms_commands WHERE request_id=?", (request_id,)).fetchone():
                return {"queued": False, "reason": "replay"}
            blocked = self.guard(conn, device, now)
            if blocked:
                return blocked
            row = conn.execute("""SELECT * FROM forwarding_operations WHERE employee_id=?
                ORDER BY id DESC LIMIT 1""", (device,)).fetchone()
            if row:
                if row["status"] in {"queued", "sending", "api_accepted", "call_started"}:
                    return {"queued": False, "reason": "busy"}
                retry = max(row["request_time"], row["completed_at"] or 0) + self.service.settings.command_cooldown_seconds - now
                if retry > 0:
                    return {"queued": False, "reason": "cooldown", "retry_after": retry}
            conn.execute("""INSERT INTO device_sms_commands
                (request_id,device_code,command,requested_at,status) VALUES (?,?,?,?,'queued')""",
                (request_id, device, COMMANDS[key][0], now))
        return {"queued": True, "reason": "queued"}

    def dispatch_one(self, now):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE device_sms_commands SET status='unconfirmed', completed_at=?,
                error='confirmation_timeout_or_process_interrupted' WHERE
                (status='sending' AND dispatched_at <= ?) OR
                (status IN ('queued','api_accepted') AND requested_at <= ?)""",
                (now, now - 60, now - self.service.settings.confirmation_timeout_seconds))
            row = conn.execute("SELECT * FROM device_sms_commands WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return {"processed": False}
            conn.execute("UPDATE device_sms_commands SET status='sending', dispatched_at=? WHERE id=?", (now, row["id"]))
        status, response, error = "api_accepted", None, None
        try:
            device = self.service.devices[row["device_code"]]
            allowed = {v[0] for k, v in COMMANDS.items() if device.code == "poco" or not k.startswith("hotspot_")}
            if row["command"] not in allowed:
                raise ValueError("Неверная сохранённая команда")
            response = json.dumps(self.service.send_sms(device.sim_number, self.service.devices["poco"].moizvonki_user,
                                                       row["command"]), ensure_ascii=False, default=str)[:5000]
            if row["command"] == "?":
                status = "location_requested"
        except (requests.Timeout, requests.ConnectionError):
            status, error = "unconfirmed", "Неизвестен результат отправки; автоповтор отключён"
        except Exception as exc:
            status, error = "api_failed", type(exc).__name__
        with self.connect() as conn:
            conn.execute("""UPDATE device_sms_commands SET status=?,response=?,error=?,completed_at=?
                WHERE id=? AND status='sending'""", (status, response, error,
                now if status != "api_accepted" else None, row["id"]))
        return {"processed": True, "status": status}

    def state(self, device):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM device_sms_commands WHERE device_code=? ORDER BY id DESC LIMIT 1", (device,)).fetchone()
        if not row:
            return {"status_label": "Команд настройки ещё не было", "reply": None}
        labels = {"queued": "SMS в очереди", "sending": "Отправляем SMS с Poco",
                  "api_accepted": "SMS принято сервисом; ждём ответ телефона",
                  "location_requested": "Запрос координат принят сервисом. Результат ищите в таблице; ожидание до 30 минут. Доставка не подтверждена.",
                  "sms_reply_received": "Получен ответ телефона — проверьте текст",
                  "unconfirmed": "Подтверждения нет; автоповтор отключён", "api_failed": "Ошибка отправки"}
        return {"command": row["command"], "status_label": labels[row["status"]], "reply": row["reply"]}

    def handle_reply(self, device, text, timestamp, now, event_key):
        if not (("Батарея:" in text and "Режим звонка:" in text) or text == "Нет интернета"):
            return False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM forwarding_sms_replies WHERE event_key=?", (event_key,)).fetchone():
                return True
            rows = conn.execute("""SELECT * FROM device_sms_commands WHERE device_code=? AND dispatched_at IS NOT NULL
                AND requested_at <= ? AND requested_at >= ?""",
                (device, timestamp, timestamp - self.service.settings.correlation_window_seconds)).fetchall()
            forward_count = conn.execute("""SELECT COUNT(*) FROM forwarding_operations WHERE employee_id=? AND attempt_count>0
                AND request_time <= ? AND request_time >= ?""",
                (device, timestamp, timestamp - self.service.settings.correlation_window_seconds)).fetchone()[0]
            if len(rows) != 1 or forward_count:
                return False
            row = rows[0]
            if text == "Нет интернета" and row["command"] != "?":
                return False
            first = text.splitlines()[0].strip()
            known_commands = {v[0] for v in COMMANDS.values()} | {"OFF", "ON Poco", "ON Redmi", "ON Tecno"}
            if first in known_commands and first != row["command"]:
                return False
            conn.execute("INSERT INTO forwarding_sms_replies VALUES (?,NULL,?,?,?)",
                         (event_key, now, self.service.devices[device].sim_number, text))
            conn.execute("""UPDATE device_sms_commands SET status='sms_reply_received',completed_at=?,reply=?,error=NULL
                WHERE id=? AND status IN ('sending','api_accepted','unconfirmed','location_requested')""", (now, text, row["id"]))
        return True
