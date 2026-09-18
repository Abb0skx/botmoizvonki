"""Run on the target host. Copy only required runtime settings, never print secrets."""
import json
import os
from pathlib import Path
import subprocess

CONTAINER = "nylgfmvjodgie9dga7ngprgl-134132395850"
TARGET = Path("/opt/texnikach-transcription-20260918/worker.env")
data = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER], text=True))[0]
environment = dict(item.split("=", 1) for item in data["Config"]["Env"] if "=" in item)
assert environment.get("MONITORING_ENABLED", "").lower() in {"true", "1", "yes", "on"}, "Web dashboard auth must stay enabled"
keys = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PUBLIC_BASE_URL", "TZ")
assert environment.get("TELEGRAM_BOT_TOKEN") and environment.get("TELEGRAM_CHAT_ID"), "Missing Telegram runtime settings"
lines = []
for key in keys:
    if key not in environment:
        continue
    value = environment[key]
    assert not any(char in value for char in "\r\n'"), "Unexpected env-file characters"
    lines.append(f"{key}='{value}'\n")
os.umask(0o077)
with TARGET.open("x", encoding="utf-8") as output:
    output.writelines(lines)
print("Private worker environment saved; existing web authentication enabled")
