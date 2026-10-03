"""Durable internal manager-assignment cards for the Business bot.

This module never sends a Business reply to a customer. Cards are delivered to
one configured private staff group, with at-most-once delivery after an
ambiguous Telegram send outcome.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from telegram_business.migrations import connect
from telegram_business.telegram_api import TelegramAPIError, TelegramBusinessAPI

from .repository import FolderRepository, iso
from .daily import is_today


LOG = logging.getLogger("telegram_folder_manager.cards")
MANAGERS = {"OLMAS": "Olmas", "OTABEK": "Otabek", "ALI": "Ali", "ABBOS": "Abbos"}
CALLBACK_RE = re.compile(r"^ma1:([1-9][0-9]{0,11}):(OLMAS|OTABEK|ALI|ABBOS)$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def manager_card_keyboard(card_id: int, customer_id: str, username: str = "") -> dict:
    rows = [
        [
            {"text": MANAGERS["OLMAS"], "callback_data": f"ma1:{card_id}:OLMAS"},
            {"text": MANAGERS["OTABEK"], "callback_data": f"ma1:{card_id}:OTABEK"},
        ],
        [
            {"text": MANAGERS["ALI"], "callback_data": f"ma1:{card_id}:ALI"},
            {"text": MANAGERS["ABBOS"], "callback_data": f"ma1:{card_id}:ABBOS"},
        ],
    ]
    link = (
        f"https://t.me/{username}"
        if USERNAME_RE.fullmatch(username or "")
        else f"tg://user?id={customer_id}"
    )
    rows.append([{"text": "Открыть клиента", "url": link}])
    return {"inline_keyboard": rows}


def _one_line(value: object, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit]


class ManagerCards:
    def __init__(
        self, repository: FolderRepository, group_chat_id: str,
        bot_token: str = "", *, api=None,
    ):
        self.repo = repository
        self.group_chat_id = str(group_chat_id)
        self.api = api or TelegramBusinessAPI(bot_token)
        self._verified_private_group = False

    def _card_details(self, customer_id: str) -> tuple[str, str]:
        with connect(self.repo.path) as db:
            row = db.execute(
                "SELECT first_name,last_name,username FROM business_clients WHERE chat_id=?",
                (customer_id,),
            ).fetchone()
        if not row:
            return f"Клиент {customer_id}", ""
        name = _one_line(" ".join(filter(None, (row["first_name"], row["last_name"]))))
        username = _one_line(row["username"], 32)
        return name or f"Клиент {customer_id}", username

    def _card_text(self, customer_id: str, manager_code: str = "") -> str:
        name, username = self._card_details(customer_id)
        handle = f"\n@{username}" if USERNAME_RE.fullmatch(username) else ""
        status = (
            f"Назначен: {MANAGERS[manager_code]}"
            if manager_code in MANAGERS else "Ожидает менеджера"
        )
        return f"Новый клиент: {name}{handle}\nID: {customer_id}\n{status}"

    def _verify_private_group(self) -> None:
        if self._verified_private_group:
            return
        chat = self.api.get_chat(self.group_chat_id)
        if (
            str(chat.get("id")) != self.group_chat_id
            or chat.get("type") not in {"group", "supergroup"}
            or chat.get("username")
        ):
            raise ValueError("manager cards destination must be a private group")
        bot_id = self.api.get_me().get("id")
        if not isinstance(bot_id, int) or bot_id <= 0:
            raise ValueError("manager card bot identity is invalid")
        bot_membership = self.api.get_chat_member(self.group_chat_id, bot_id)
        if bot_membership.get("status") not in {"creator", "administrator"}:
            raise ValueError("manager card bot must be a group administrator")
        self._verified_private_group = True

    def _claim(self, now: datetime):
        stamp = iso(now)
        with connect(self.repo.path) as db:
            db.execute("BEGIN IMMEDIATE")
            # A send in progress when the process died may already be visible
            # in Telegram. Never replay it automatically.
            db.execute(
                """UPDATE telegram_manager_cards
                      SET status='unknown',lease_token=NULL,lease_expires_at=NULL,
                          updated_at=?,last_error='send outcome unknown after restart'
                    WHERE status='sending' AND lease_expires_at<=?""",
                (stamp, stamp),
            )
            row = db.execute(
                """SELECT * FROM telegram_manager_cards
                    WHERE status IN ('pending','retry') AND next_attempt_at<=?
                    ORDER BY card_id LIMIT 1""",
                (stamp,),
            ).fetchone()
            if not row:
                return None
            assignment = db.execute(
                "SELECT folder_code,revision FROM telegram_folder_assignments WHERE chat_id=?",
                (row["chat_id"],),
            ).fetchone()
            if (
                not assignment or assignment["folder_code"] != "NEW"
                or not is_today(row["created_at"], now)
                or int(assignment["revision"]) != int(row["assignment_revision"])
                or row["group_chat_id"] != self.group_chat_id
            ):
                db.execute(
                    "UPDATE telegram_manager_cards SET status='cancelled',updated_at=? WHERE card_id=?",
                    (stamp, row["card_id"]),
                )
                return None
            token = secrets.token_urlsafe(24)
            db.execute(
                """UPDATE telegram_manager_cards SET status='sending',attempts=attempts+1,
                   lease_token=?,lease_expires_at=?,updated_at=? WHERE card_id=?""",
                (token, iso(now + timedelta(minutes=2)), stamp, row["card_id"]),
            )
            return dict(row), token

    def _complete(
        self, card_id: int, token: str, status: str, now: datetime, *,
        message_id: int | None = None, error: str = "", retry_after: float | None = None,
    ) -> None:
        with connect(self.repo.path) as db:
            row = db.execute(
                "SELECT attempts FROM telegram_manager_cards WHERE card_id=? AND status='sending' AND lease_token=?",
                (card_id, token),
            ).fetchone()
            if row is None:
                return
            if status == "retry" and int(row["attempts"]) >= 12:
                status = "failed"
            delay = max(1.0, float(retry_after)) if retry_after is not None else min(3600, 2 ** int(row["attempts"]))
            db.execute(
                """UPDATE telegram_manager_cards SET status=?,group_message_id=?,
                   next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,
                   last_error=?,updated_at=? WHERE card_id=? AND lease_token=?""",
                (
                    status, message_id,
                    iso(now + timedelta(seconds=delay)) if status == "retry" else iso(now),
                    _one_line(error, 300), iso(now), card_id, token,
                ),
            )

    def dispatch_due(self, now: datetime, *, limit: int = 1) -> int:
        sent = 0
        for _ in range(max(1, min(limit, 20))):
            claimed = self._claim(now)
            if claimed is None:
                break
            row, token = claimed
            card_id = int(row["card_id"])
            attempted_send = False
            try:
                self._verify_private_group()
                name, username = self._card_details(row["chat_id"])
                del name
                attempted_send = True
                response = self.api.send_manager_card(
                    self.group_chat_id,
                    self._card_text(row["chat_id"]),
                    manager_card_keyboard(card_id, row["chat_id"], username),
                )
                message_id = (response.get("result") or {}).get("message_id")
                if not isinstance(message_id, int) or message_id <= 0:
                    raise TelegramAPIError("invalid manager card response", ambiguous=True)
            except TelegramAPIError as exc:
                safe_retry = exc.status == 429 or not attempted_send or (
                    not exc.ambiguous and exc.status in {400, 403}
                )
                self._complete(
                    card_id, token, "retry" if safe_retry else "unknown", now,
                    error=type(exc).__name__, retry_after=exc.retry_after,
                )
                LOG.warning("manager_card_delivery_failed card_id=%s type=%s", card_id, type(exc).__name__)
            except ValueError as exc:
                self._complete(card_id, token, "failed", now, error=type(exc).__name__)
                LOG.error("manager_card_destination_invalid card_id=%s", card_id)
            except Exception as exc:
                self._complete(
                    card_id, token, "unknown" if attempted_send else "retry", now,
                    error=type(exc).__name__,
                )
                LOG.error("manager_card_delivery_error card_id=%s type=%s", card_id, type(exc).__name__)
            else:
                self._complete(card_id, token, "sent", now, message_id=message_id)
                sent += 1
        return sent

    def handle_callback(self, update: dict, now: datetime) -> bool:
        query = update.get("callback_query") or {}
        data = str(query.get("data") or "")
        if not data.startswith("ma1:"):
            return False
        callback_id = str(query.get("id") or "")
        message = query.get("message") or {}
        group_id = str((message.get("chat") or {}).get("id") or "")
        message_id = message.get("message_id")
        actor = query.get("from") or {}
        match = CALLBACK_RE.fullmatch(data)
        if (
            not match or not callback_id or group_id != self.group_chat_id
            or not isinstance(message_id, int)
            or not isinstance(actor.get("id"), int) or actor.get("id") <= 0
            or actor.get("is_bot")
        ):
            if callback_id:
                self.api.answer_callback_query(callback_id, text="Кнопка недоступна", show_alert=True)
            return True

        membership = self.api.get_chat_member(group_id, int(actor["id"]))
        if membership.get("status") not in {"creator", "administrator", "member"}:
            self.api.answer_callback_query(callback_id, text="Нет доступа", show_alert=True)
            return True

        card_id, manager_code = int(match[1]), match[2]
        with connect(self.repo.path) as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute(
                "SELECT outcome FROM telegram_manager_card_callbacks WHERE callback_query_id=?",
                (callback_id,),
            ).fetchone()
            card = db.execute(
                """SELECT chat_id,assignment_revision,created_at FROM telegram_manager_cards WHERE card_id=?
                   AND status='sent' AND group_chat_id=? AND group_message_id=?""",
                (card_id, group_id, message_id),
            ).fetchone()
            if card and not is_today(card["created_at"], now):
                card = None
            if receipt:
                outcome = str(receipt["outcome"])
                customer_id = str(card["chat_id"]) if card else None
            elif not card:
                outcome = "Кнопка устарела"
                customer_id = None
            else:
                customer_id = str(card["chat_id"])
                supplier = db.execute(
                    "SELECT 1 FROM telegram_supplier_group_members WHERE user_id=?",
                    (customer_id,),
                ).fetchone()
                latest = db.execute(
                    "SELECT MAX(card_id) FROM telegram_manager_cards WHERE chat_id=?",
                    (customer_id,),
                ).fetchone()[0]
                current = db.execute(
                    "SELECT folder_code,revision FROM telegram_folder_assignments WHERE chat_id=?",
                    (customer_id,),
                ).fetchone()
                if (
                    supplier or latest != card_id or not current
                    or current["folder_code"] not in {"NEW", *MANAGERS}
                    or (
                        current["folder_code"] == "NEW"
                        and int(current["revision"]) != int(card["assignment_revision"])
                    )
                ):
                    outcome = "Клиент больше не ожидает назначения"
                else:
                    self.repo._assign_in_db(
                        db, customer_id, manager_code, now,
                        source="telegram_manager_button",
                        assigned_by_id=str(actor["id"]),
                        assigned_by_name=_one_line(actor.get("first_name"), 80),
                    )
                    outcome = f"Назначен: {MANAGERS[manager_code]}"
            if not receipt:
                db.execute(
                    """INSERT INTO telegram_manager_card_callbacks(
                         callback_query_id,card_id,manager_code,outcome,created_at)
                       VALUES(?,?,?,?,?)""",
                    (callback_id, card_id, manager_code, outcome, iso(now)),
                )

        self.api.answer_callback_query(callback_id, text=outcome)
        if customer_id and outcome.startswith("Назначен:"):
            try:
                current = self.repo.assignment(customer_id)
                current_code = current["folder_code"] if current else ""
                _, username = self._card_details(customer_id)
                self.api.edit_manager_card(
                    group_id, message_id,
                    self._card_text(customer_id, current_code),
                    manager_card_keyboard(card_id, customer_id, username),
                )
            except Exception as exc:
                # The database assignment and durable folder job are already
                # committed. A cosmetic edit failure must not undo them.
                LOG.warning("manager_card_edit_failed card_id=%s type=%s", card_id, type(exc).__name__)
        return True
