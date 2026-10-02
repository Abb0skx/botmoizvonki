import asyncio
import inspect
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

from telegram_business.migrations import connect
from telegram_folder_manager.auth import credentials_from_page
from telegram_folder_manager.config import DEFAULT_COLORS, FOLDER_CODES, FolderSettings, FolderSpec
from telegram_folder_manager.gateway import TelegramFolderGateway
from telegram_folder_manager.repository import FolderRepository
from telegram_folder_manager.service import TelegramFolderService
from telegram_folder_manager.suppliers import scan_supplier_groups
from telegram_business.repository import BusinessRepository


NOW = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


def settings(db_path: Path) -> FolderSettings:
    return FolderSettings(
        enabled=True,
        api_id=12345,
        api_hash="a" * 32,
        session_path=db_path.with_suffix(".session"),
        db_path=db_path,
        poll_seconds=5,
        reconcile_seconds=30,
        lease_seconds=120,
        max_attempts=4,
        backfill_existing=False,
        reopen_done=True,
        folders=tuple(
            FolderSpec(code, code, DEFAULT_COLORS[code]) for code in FOLDER_CODES
        ),
    )


def add_client_message(repo: FolderRepository, chat_id: str, message_id: int) -> None:
    stamp = NOW.isoformat()
    with connect(repo.path) as db:
        db.execute(
            """INSERT INTO business_messages(
                 business_connection_id,chat_id,message_id,direction,sender_type,
                 message_type,created_at)
               VALUES('connection',?,?,'incoming','client','text',?)""",
            (chat_id, message_id, stamp),
        )


def add_business_client(repo: FolderRepository, chat_id: str) -> None:
    with connect(repo.path) as db:
        db.execute(
            """INSERT INTO business_clients(chat_id,created_at,updated_at,last_client_message_at)
               VALUES(?,?,?,?)""",
            (chat_id, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )


def test_credentials_page_is_parsed_without_environment_format(tmp_path):
    page = tmp_path / "app.txt"
    page.write_text(
        "App api_id\n12345678\nApp api_hash\n0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    assert credentials_from_page(page) == (
        12345678,
        "0123456789abcdef0123456789abcdef",
    )


def test_first_start_does_not_backfill_old_chats(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    add_client_message(repo, "1001", 1)

    assert repo.seed_new_clients(NOW, backfill_existing=False) == 0
    assert repo.assignments() == []

    add_client_message(repo, "1002", 2)
    assert repo.seed_new_clients(NOW, backfill_existing=False) == 1
    row = repo.assignment("1002")
    assert row["folder_code"] == "NEW"
    jobs = repo.jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "pending"


def test_backfill_is_explicit_and_idempotent(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    add_client_message(repo, "1001", 1)
    add_client_message(repo, "1001", 2)
    add_client_message(repo, "1002", 3)

    assert repo.seed_new_clients(NOW, backfill_existing=True) == 2
    assert repo.seed_new_clients(NOW, backfill_existing=True) == 0
    assert {row["chat_id"] for row in repo.assignments()} == {"1001", "1002"}
    assert len(repo.jobs()) == 2


def test_new_message_reopens_done_but_keeps_active_manager(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    repo.seed_new_clients(NOW)
    repo.assign("1001", "DONE", NOW)
    repo.assign("1002", "OLMAS", NOW)
    add_client_message(repo, "1001", 1)
    add_client_message(repo, "1002", 2)

    assert repo.seed_new_clients(NOW, reopen_done=True) == 1
    assert repo.assignment("1001")["folder_code"] == "NEW"
    assert repo.assignment("1002")["folder_code"] == "OLMAS"


def test_supplier_group_members_are_split_and_pause_business_bot(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    for chat_id in ("1001", "1002", "1003", "1004", "1005"):
        add_business_client(repo, chat_id)
    members = {chat_id: "-1001" for chat_id in ("1001", "1002", "1003", "1004", "1005")}

    assigned, released, overflow, to_pause, matched = repo.sync_supplier_members(
        members, NOW, complete=True, folder_capacity=1,
    )
    assert matched == 5
    assert len(assigned) == 4
    assert released == []
    assert overflow == ["1005"]
    assert set(to_pause) == {"1001", "1002", "1003", "1004", "1005"}
    assert {repo.assignment(chat_id)["folder_code"] for chat_id in assigned} == {
        "SUPPLIER", "SUPPLIER2", "SUPPLIER3", "SUPPLIER4",
    }
    assert repo.is_supplier_member("1005")
    # The cached group membership stops an answer even if a folder is full.
    assert not BusinessRepository(repo.path).may_automate("1005", NOW)


def test_new_supplier_chat_uses_cached_membership_instead_of_new(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    repo.seed_new_clients(NOW)
    repo.sync_supplier_members(
        {"1001": "-1001"}, NOW, complete=True, folder_capacity=199,
    )
    add_business_client(repo, "1001")
    add_client_message(repo, "1001", 1)

    assert repo.seed_new_clients(NOW) == 1
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"
    client = BusinessRepository(repo.path).client("1001")
    assert client["bot_paused"] == 1
    assert client["pause_reason"] == "supplier_group"


def test_old_private_dialog_is_classified_without_business_history(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    assigned, released, overflow, to_pause, matched = repo.sync_supplier_members(
        {"1001": "-1001"}, NOW, complete=True, folder_capacity=199,
        private_dialog_ids={"1001"},
    )
    assert (assigned, released, overflow, to_pause, matched) == (
        ["1001"], [], [], [], 1,
    )
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"


def test_supplier_leaving_all_groups_is_released_only_after_complete_scan(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    add_business_client(repo, "1001")
    repo.sync_supplier_members(
        {"1001": "-1001"}, NOW, complete=True, folder_capacity=199,
    )
    _, released, _, _, _ = repo.sync_supplier_members(
        {}, NOW, complete=False, folder_capacity=199,
    )
    assert released == []
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"
    _, released, _, _, _ = repo.sync_supplier_members(
        {}, NOW, complete=True, folder_capacity=199,
    )
    assert released == ["1001"]
    assert repo.assignment("1001")["folder_code"] == "NEW"


def test_supplier_scan_uses_ids_and_requires_complete_group_lists():
    class Client:
        async def iter_dialogs(self, limit):
            yield SimpleNamespace(id=1001, is_user=True, is_group=False)
            yield SimpleNamespace(
                id=-1001, is_user=False, is_group=True, input_entity="group",
                entity=SimpleNamespace(participants_count=2),
            )
        async def iter_participants(self, entity):
            yield SimpleNamespace(id=1001)

    scan = asyncio.run(scan_supplier_groups(Client(), (-1001, -1002)))
    assert scan.members == {"1001": "-1001"}
    assert scan.private_dialog_ids == frozenset({"1001"})
    assert not scan.complete
    assert "-1002" in scan.unavailable
    assert any(item.startswith("-1001:partial_") for item in scan.unavailable)


def test_stale_new_tag_cannot_override_supplier_assignment(tmp_path):
    config = settings(tmp_path / "business.db")
    repo = FolderRepository(config.db_path)
    add_business_client(repo, "1001")
    repo.assign("1001", "NEW", NOW)
    initial = repo.claim_due(NOW)[0]
    repo.finish(initial["job_id"], initial["lease_token"], NOW)
    repo.sync_supplier_members(
        {"1001": "-1001"}, NOW, complete=True, folder_capacity=199,
    )
    gateway = FakeGateway({"1001": {"NEW"}})
    service = TelegramFolderService(
        config, repository=repo, gateway=gateway, clock=lambda: NOW,
    )

    assert asyncio.run(service.reconcile_manual_moves()) == 0
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"
    pending = repo.claim_due(NOW)[0]
    repo.finish(pending["job_id"], pending["lease_token"], NOW)
    assert asyncio.run(service.reconcile_manual_moves()) == 1
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"
    assert repo.jobs()[-1]["state"] == "pending"


def test_supplier_folder_migration_preserves_old_assignments_and_jobs(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE telegram_folder_assignments (
              chat_id TEXT PRIMARY KEY, folder_code TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL,
              assigned_by_id TEXT, assigned_by_name TEXT,
              assigned_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              CHECK(folder_code IN ('NEW','OLMAS','OTABEK','ALI','ABBOS','DONE')));
            CREATE TABLE telegram_folder_jobs (
              job_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
              folder_code TEXT NOT NULL, revision INTEGER NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL, lease_token TEXT, lease_expires_at TEXT,
              last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              completed_at TEXT, UNIQUE(chat_id,revision),
              CHECK(folder_code IN ('NEW','OLMAS','OTABEK','ALI','ABBOS','DONE')),
              CHECK(state IN ('pending','running','retry','done','failed','superseded')));
        """)
        stamp = NOW.isoformat()
        db.execute(
            """INSERT INTO telegram_folder_assignments
               (chat_id,folder_code,revision,source,assigned_at,updated_at)
               VALUES('1001','NEW',1,'legacy',?,?)""", (stamp, stamp),
        )
        db.execute(
            """INSERT INTO telegram_folder_jobs
               (job_id,chat_id,folder_code,revision,state,next_attempt_at,created_at,updated_at)
               VALUES(7,'1001','NEW',1,'done',?,?,?)""", (stamp, stamp, stamp),
        )

    repo = FolderRepository(path)
    assert repo.assignment("1001")["folder_code"] == "NEW"
    assert repo.jobs()[0]["job_id"] == 7
    repo.assign("1001", "SUPPLIER", NOW)
    assert repo.assignment("1001")["folder_code"] == "SUPPLIER"
    assert [row["job_id"] for row in repo.jobs()] == [7, 8]
    assert FolderRepository(path).assignment("1001")["folder_code"] == "SUPPLIER"


def test_newer_assignment_supersedes_old_job(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    first = repo.assign("1001", "NEW", NOW)
    second = repo.assign("1001", "ABBOS", NOW)

    assert second["revision"] == first["revision"] + 1
    jobs = repo.jobs()
    assert [row["state"] for row in jobs] == ["superseded", "pending"]
    claimed = repo.claim_due(NOW)
    assert len(claimed) == 1
    assert claimed[0]["folder_code"] == "ABBOS"
    assert repo.is_current(claimed[0])


def test_retry_is_durable_and_redacts_long_secrets(tmp_path):
    repo = FolderRepository(tmp_path / "business.db")
    repo.assign("1001", "NEW", NOW)
    row = repo.claim_due(NOW)[0]
    error = RuntimeError("failed " + "a" * 32)
    assert repo.retry(row["job_id"], row["lease_token"], error, NOW)
    saved = repo.jobs()[0]
    assert saved["state"] == "retry"
    assert "a" * 32 not in saved["last_error"]
    assert "[redacted]" in saved["last_error"]


class FakeGateway:
    def __init__(self, memberships=None, *, fail=False):
        self.memberships = memberships or {}
        self.fail = fail
        self.moved = []

    async def connect(self):
        return None

    async def ensure_folders(self):
        return None

    async def disconnect(self):
        return None

    async def move(self, chat_id, folder_code):
        if self.fail:
            raise TimeoutError("temporary")
        self.moved.append((chat_id, folder_code))

    async def snapshot(self):
        return self.memberships


class FakeMTProtoClient:
    def __init__(self):
        self.filters = []
        self.tags_enabled = False
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_input_entity(self, value):
        from telethon.tl import types

        if value == "me":
            return types.InputPeerUser(999, 999)
        return types.InputPeerUser(int(value), int(value) * 10)

    async def __call__(self, request):
        name = type(request).__name__
        if name == "GetDialogFiltersRequest":
            return SimpleNamespace(
                filters=list(self.filters), tags_enabled=self.tags_enabled
            )
        if name == "ToggleDialogFilterTagsRequest":
            self.tags_enabled = bool(request.enabled)
            return True
        if name == "UpdateDialogFilterRequest":
            self.filters = [
                folder for folder in self.filters if int(folder.id) != int(request.id)
            ]
            if request.filter is not None:
                self.filters.append(request.filter)
            return True
        raise AssertionError(name)

    async def iter_dialogs(self):
        if False:
            yield None


def test_service_finishes_successful_job(tmp_path):
    config = settings(tmp_path / "business.db")
    repo = FolderRepository(config.db_path)
    repo.assign("1001", "OLMAS", NOW)
    gateway = FakeGateway()
    service = TelegramFolderService(
        config, repository=repo, gateway=gateway, clock=lambda: NOW
    )

    assert asyncio.run(service.process_jobs()) == 1
    assert gateway.moved == [("1001", "OLMAS")]
    assert repo.jobs()[0]["state"] == "done"


def test_manual_manager_folder_wins_over_new_and_is_normalized(tmp_path):
    config = settings(tmp_path / "business.db")
    repo = FolderRepository(config.db_path)
    repo.assign("1001", "NEW", NOW)
    # Apply and finish the initial NEW job so reconciliation creates exactly
    # one fresh normalization job.
    initial = repo.claim_due(NOW)[0]
    repo.finish(initial["job_id"], initial["lease_token"], NOW)
    gateway = FakeGateway({"1001": {"NEW", "OTABEK"}})
    service = TelegramFolderService(
        config, repository=repo, gateway=gateway, clock=lambda: NOW
    )

    assert asyncio.run(service.reconcile_manual_moves()) == 1
    assert repo.assignment("1001")["folder_code"] == "OTABEK"
    assert repo.jobs()[-1]["state"] == "pending"


def test_multiple_manager_folders_are_not_guessed(tmp_path):
    config = settings(tmp_path / "business.db")
    repo = FolderRepository(config.db_path)
    gateway = FakeGateway({"1001": {"OLMAS", "ABBOS"}})
    service = TelegramFolderService(
        config, repository=repo, gateway=gateway, clock=lambda: NOW
    )

    assert asyncio.run(service.reconcile_manual_moves()) == 0
    assert repo.assignment("1001") is None


def test_gateway_has_no_message_send_or_read_history_code_path():
    source = inspect.getsource(TelegramFolderGateway)
    assert "send_message" not in source
    assert "ReadHistoryRequest" not in source
    assert "read_history" not in source


def test_gateway_creates_colored_folders_and_moves_one_private_chat(tmp_path):
    config = settings(tmp_path / "business.db")
    client = FakeMTProtoClient()
    gateway = TelegramFolderGateway(config, client=client)

    async def scenario():
        await gateway.connect()
        await gateway.ensure_folders()
        await gateway.move("1001", "NEW")
        assert await gateway.snapshot() == {"1001": {"NEW"}}
        await gateway.move("1001", "OLMAS")
        return await gateway.snapshot()

    snapshot = asyncio.run(scenario())
    assert client.tags_enabled is True
    assert len(client.filters) == len(FOLDER_CODES)
    assert {folder.title.text: folder.color for folder in client.filters} == {
        code: DEFAULT_COLORS[code] for code in FOLDER_CODES
    }
    assert snapshot == {"1001": {"OLMAS"}}


def test_gateway_preserves_case_distinct_personal_folder_and_ignores_channels(tmp_path):
    from telethon.tl import types

    config = settings(tmp_path / "business.db")
    client = FakeMTProtoClient()
    personal_channel = types.InputPeerChannel(123, 456)
    personal_folder = types.DialogFilter(
        id=102,
        title=types.TextWithEntities("Ali", []),
        pinned_peers=[],
        include_peers=[personal_channel],
        exclude_peers=[],
        color=2,
    )
    client.filters = [personal_folder]
    gateway = TelegramFolderGateway(config, client=client)

    async def scenario():
        await gateway.connect()
        await gateway.ensure_folders()
        managed_ali = next(
            folder for folder in client.filters if gateway._title(folder) == "ALI"
        )
        managed_ali.include_peers.append(types.InputPeerChannel(789, 987))
        return await gateway.snapshot()

    snapshot = asyncio.run(scenario())
    titles = [gateway._title(folder) for folder in client.filters]
    assert titles.count("Ali") == 1
    assert titles.count("ALI") == 1
    assert personal_folder.include_peers == [personal_channel]
    assert snapshot == {}
