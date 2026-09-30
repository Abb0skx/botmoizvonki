from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
from pathlib import Path


def _next_matching(lines: list[str], start: int, pattern: str) -> str:
    regex = re.compile(pattern)
    for line in lines[start + 1:]:
        value = line.strip()
        if regex.fullmatch(value):
            return value
    return ""


def credentials_from_page(path: Path) -> tuple[int, str]:
    """Read a copied my.telegram.org page without ever printing its secrets."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    api_id = ""
    api_hash = ""
    for index, line in enumerate(lines):
        label = line.strip().casefold().replace("-", "_")
        if label.startswith("app api_id"):
            api_id = _next_matching(lines, index, r"[1-9][0-9]{3,15}")
        elif label.startswith("app api_hash"):
            api_hash = _next_matching(lines, index, r"[0-9a-fA-F]{32}")
    if not api_id or not api_hash:
        raise ValueError("api_id/api_hash were not found in the credentials file")
    return int(api_id), api_hash


def _credentials(path: Path | None) -> tuple[int, str]:
    if path is not None:
        return credentials_from_page(path)
    api_id = os.getenv("TELEGRAM_USER_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_USER_API_HASH", "").strip()
    if not api_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("set TELEGRAM_USER_API_ID and TELEGRAM_USER_API_HASH")
    return int(api_id), api_hash


def _print_qr(url: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def authorize(session_path: Path, api_id: int, api_hash: str) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    old_umask = os.umask(0o077)
    try:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session_path), api_id, api_hash,
            device_model="TEXNIKACH Folder Manager",
            system_version="server", app_version="1.0",
            receive_updates=False,
        )
        client.session.save_entities = False
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"AUTHORIZED account_id={me.id} username={me.username or '-'}")
            await client.disconnect()
            return

        print("Telegram → Settings → Devices → Link Desktop Device; scan this QR:")
        for _ in range(5):
            login = await client.qr_login()
            _print_qr(login.url)
            try:
                await login.wait(timeout=55)
                break
            except asyncio.TimeoutError:
                print("QR expired; generating a new one...")
            except SessionPasswordNeededError:
                password = getpass.getpass("Telegram 2FA password: ")
                await client.sign_in(password=password)
                break
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram QR authorization timed out")
        me = await client.get_me()
        print(f"AUTHORIZED account_id={me.id} username={me.username or '-'}")
        await client.disconnect()
        for candidate in session_path.parent.glob(session_path.name + "*"):
            candidate.chmod(0o600)
    finally:
        os.umask(old_umask)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize the TEXNIKACH Telegram folder manager by QR"
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=Path(os.getenv(
            "TELEGRAM_USER_SESSION_PATH", "/app/data/texnikach-user.session"
        )),
    )
    parser.add_argument(
        "--credentials-file", type=Path,
        help="Copied my.telegram.org page; its contents are never printed",
    )
    args = parser.parse_args()
    api_id, api_hash = _credentials(args.credentials_file)
    asyncio.run(authorize(args.session, api_id, api_hash))


if __name__ == "__main__":
    main()
