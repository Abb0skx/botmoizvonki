from datetime import datetime, timezone
from dataclasses import replace

from telegram_business.config import BusinessSettings
from telegram_business.migrations import connect
from telegram_business.service import BusinessService
from telegram_business.telegram_api import TelegramAPIError
from telegram_folder_manager.cards import ManagerCards
from telegram_folder_manager.repository import FolderRepository


NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
GROUP = "-1001234567890"


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []
        self.member_status = "member"
        self.bot_status = "administrator"
        self.chat_type = "supergroup"
        self.username = None
        self.send_error = None

    def get_chat(self, chat_id):
        return {
            "id": int(chat_id), "type": self.chat_type,
            "username": self.username,
        }

    def get_chat_member(self, chat_id, user_id):
        return {"status": self.bot_status if user_id == 123 else self.member_status}

    def get_me(self):
        return {"id": 123}

    def send_manager_card(self, chat_id, text, reply_markup):
        self.sent.append((chat_id, text, reply_markup))
        if self.send_error:
            raise self.send_error
        return {"result": {"message_id": 100}}

    def edit_manager_card(self, chat_id, message_id, text, reply_markup):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return {"result": {"message_id": message_id}}

    def answer_callback_query(self, callback_id, **options):
        self.answered.append((callback_id, options))
        return {"result": True}


def create_new_client(tmp_path, *, group=GROUP):
    repo = FolderRepository(tmp_path / "business.db")
    assert repo.seed_new_clients(NOW, manager_cards_chat_id=group) == 0
    with connect(repo.path) as db:
        db.execute(
            """INSERT INTO business_clients(chat_id,first_name,username,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            ("1001", "Client <name>", "client_user", NOW.isoformat(), NOW.isoformat()),
        )
        db.execute(
            """INSERT INTO business_messages(
                 business_connection_id,chat_id,message_id,direction,sender_type,
                 message_type,text,created_at)
               VALUES('connection','1001',1,'incoming','client','text',
                      'secret customer message',?)""",
            (NOW.isoformat(),),
        )
    assert repo.seed_new_clients(NOW, manager_cards_chat_id=group) == 1
    return repo


def card_row(repo):
    with connect(repo.path) as db:
        return db.execute("SELECT * FROM telegram_manager_cards").fetchone()


def click(card_id, code, *, group=GROUP, callback_id="callback-1"):
    return {
        "callback_query": {
            "id": callback_id,
            "data": f"ma1:{card_id}:{code}",
            "from": {"id": 700, "first_name": "Operator"},
            "message": {"chat": {"id": int(group)}, "message_id": 100},
        }
    }


def test_new_client_card_is_durable_and_not_sent_twice(tmp_path):
    repo = create_new_client(tmp_path)
    assert repo.seed_new_clients(NOW, manager_cards_chat_id=GROUP) == 0
    api = FakeAPI()
    cards = ManagerCards(repo, GROUP, api=api)

    assert cards.dispatch_due(NOW) == 1
    assert cards.dispatch_due(NOW) == 0
    assert card_row(repo)["status"] == "sent"
    assert card_row(repo)["group_message_id"] == 100
    assert len(api.sent) == 1
    assert "secret customer message" not in api.sent[0][1]
    assert "Client <name>" in api.sent[0][1]
    buttons = api.sent[0][2]["inline_keyboard"]
    assert [item["text"] for row in buttons[:2] for item in row] == [
        "Olmas", "Otabek", "Ali", "Abbos",
    ]
    assert buttons[-1][0]["url"] == "https://t.me/client_user"


def test_button_assigns_manager_and_repeat_button_reassigns(tmp_path):
    repo = create_new_client(tmp_path)
    api = FakeAPI()
    cards = ManagerCards(repo, GROUP, api=api)
    assert cards.dispatch_due(NOW) == 1
    card_id = card_row(repo)["card_id"]

    assert cards.handle_callback(click(card_id, "OLMAS"), NOW)
    assignment = repo.assignment("1001")
    assert assignment["folder_code"] == "OLMAS"
    assert assignment["assigned_by_id"] == "700"
    assert assignment["source"] == "telegram_manager_button"
    assert api.edited[-1][2].endswith("Назначен: Olmas")

    # A retry of the same Telegram callback must not reverse a later choice.
    assert cards.handle_callback(click(card_id, "ALI", callback_id="callback-2"), NOW)
    assert repo.assignment("1001")["folder_code"] == "ALI"
    assert cards.handle_callback(click(card_id, "OLMAS"), NOW)
    assert repo.assignment("1001")["folder_code"] == "ALI"
    assert api.edited[-1][2].endswith("Назначен: Ali")


def test_group_and_membership_are_checked_before_assignment(tmp_path):
    repo = create_new_client(tmp_path)
    api = FakeAPI()
    cards = ManagerCards(repo, GROUP, api=api)
    cards.dispatch_due(NOW)
    card_id = card_row(repo)["card_id"]

    assert cards.handle_callback(click(card_id, "OLMAS", group="-1009999999999"), NOW)
    assert repo.assignment("1001")["folder_code"] == "NEW"
    api.member_status = "left"
    assert cards.handle_callback(click(card_id, "OLMAS", callback_id="callback-2"), NOW)
    assert repo.assignment("1001")["folder_code"] == "NEW"


def test_supplier_or_closed_chat_cannot_be_reassigned_from_old_card(tmp_path):
    repo = create_new_client(tmp_path)
    api = FakeAPI()
    cards = ManagerCards(repo, GROUP, api=api)
    cards.dispatch_due(NOW)
    card_id = card_row(repo)["card_id"]

    repo.assign("1001", "DONE", NOW)
    cards.handle_callback(click(card_id, "ALI"), NOW)
    assert repo.assignment("1001")["folder_code"] == "DONE"

    repo.assign("1001", "NEW", NOW)
    cards.handle_callback(click(card_id, "ALI", callback_id="callback-2"), NOW)
    assert repo.assignment("1001")["folder_code"] == "NEW"
    with connect(repo.path) as db:
        db.execute(
            "INSERT INTO telegram_supplier_group_members(user_id,group_id,updated_at) VALUES(?,?,?)",
            ("1001", "-1009", NOW.isoformat()),
        )
    cards.handle_callback(click(card_id, "ALI", callback_id="callback-3"), NOW)
    assert repo.assignment("1001")["folder_code"] == "NEW"


def test_ambiguous_send_is_not_replayed_after_restart(tmp_path):
    repo = create_new_client(tmp_path)
    api = FakeAPI()
    api.send_error = TelegramAPIError("timeout", retryable=True, ambiguous=True)
    cards = ManagerCards(repo, GROUP, api=api)

    assert cards.dispatch_due(NOW) == 0
    assert card_row(repo)["status"] == "unknown"
    assert ManagerCards(repo, GROUP, api=FakeAPI()).dispatch_due(NOW) == 0


def test_rate_limit_is_retried_but_public_group_is_rejected(tmp_path):
    repo = create_new_client(tmp_path)
    api = FakeAPI()
    api.send_error = TelegramAPIError("rate limit", status=429, retryable=True, retry_after=5)
    cards = ManagerCards(repo, GROUP, api=api)
    assert cards.dispatch_due(NOW) == 0
    assert card_row(repo)["status"] == "retry"
    api.send_error = None
    assert cards.dispatch_due(NOW) == 0
    assert cards.dispatch_due(NOW.replace(second=10)) == 1

    second = create_new_client(tmp_path / "another")
    public_api = FakeAPI()
    public_api.username = "public_group"
    assert ManagerCards(second, GROUP, api=public_api).dispatch_due(NOW) == 0
    assert card_row(second)["status"] == "failed"
    assert public_api.sent == []

    third = create_new_client(tmp_path / "third")
    ordinary_bot = FakeAPI()
    ordinary_bot.bot_status = "member"
    assert ManagerCards(third, GROUP, api=ordinary_bot).dispatch_due(NOW) == 0
    assert card_row(third)["status"] == "failed"
    assert ordinary_bot.sent == []


def test_missing_group_or_already_assigned_client_produces_no_card(tmp_path):
    repo = create_new_client(tmp_path, group="")
    with connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_manager_cards").fetchone()[0] == 0

    another = create_new_client(tmp_path / "another")
    another.assign("1001", "ALI", NOW)
    api = FakeAPI()
    assert ManagerCards(another, GROUP, api=api).dispatch_due(NOW) == 0
    assert card_row(another)["status"] == "cancelled"
    assert api.sent == []


def test_regular_group_callback_routes_through_business_webhook_worker(tmp_path):
    from datetime import time

    repo = create_new_client(tmp_path)
    api = FakeAPI()
    settings = BusinessSettings(
        False, "123:token", "secret", "connection", "", "Asia/Tashkent",
        time(20), time(9, 30), time(10), time(20),
        300, 3, 120, 720, 4, 8, repo.path, "sheet", 60, 300,
        "existing_google_bot_prices", "", 1440,
    )
    service = BusinessService(
        replace(settings, manager_assignments_chat_id=GROUP),
        clock=lambda: NOW, api=api, products=object(),
    )
    assert service.manager_cards.dispatch_due(NOW) == 1
    update = {"update_id": 44, **click(card_row(repo)["card_id"], "ABBOS")}
    assert service.repo.save_update(update, NOW, allowed_connection_id="connection")
    service.requests.handle_callback = lambda *_: (_ for _ in ()).throw(AssertionError("wizard called"))

    service.process_update(update)
    assert repo.assignment("1001")["folder_code"] == "ABBOS"
    assert service.repo.update(44)["status"] == "processed"
