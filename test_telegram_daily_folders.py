import asyncio
from datetime import datetime, timedelta, timezone

from telegram_business.migrations import connect
from telegram_folder_manager.daily import DAILY_CODES
from telegram_folder_manager.repository import FolderRepository
from telegram_folder_manager.gateway import TelegramFolderGateway
from telegram_folder_manager.service import TelegramFolderService
from telegram_folder_manager.cards import ManagerCards
from test_telegram_folder_manager import settings, FakeMTProtoClient
from test_telegram_manager_cards import FakeAPI, GROUP, click

BEFORE = datetime(2026, 10, 3, 18, 59, 59, tzinfo=timezone.utc)
MIDNIGHT = BEFORE + timedelta(seconds=1)  # 00:00 Asia/Tashkent


def incoming(repo, chat, message_id, at, *, received=None):
    with connect(repo.path) as db:
        db.execute(
            """INSERT INTO business_messages(business_connection_id,chat_id,message_id,
               direction,sender_type,message_type,telegram_date,created_at)
               VALUES('test',?,?,'incoming','client','text',?,?)""",
            (chat, message_id, at.isoformat(), (received or at).isoformat()),
        )


def prepare(path):
    repo = FolderRepository(path)
    repo.begin_day(BEFORE)
    repo.seed_new_clients(BEFORE)
    return repo


def test_midnight_expires_only_daily_assignments_without_deleting_messages(tmp_path):
    repo = prepare(tmp_path / 'business.db')
    for index, code in enumerate((*DAILY_CODES, 'SUPPLIER', 'SUPPLIER2', 'DONE'), 1001):
        repo.assign(str(index), code, BEFORE)
        incoming(repo, str(index), index, BEFORE)
    old_jobs = repo.claim_due(BEFORE)
    assert repo.begin_day(BEFORE) is None
    assert repo.begin_day(MIDNIGHT) == '2026-10-04'
    assert {r['folder_code'] for r in repo.assignments()} == {'SUPPLIER', 'SUPPLIER2', 'DONE'}
    assert all(not repo.is_current(j) for j in old_jobs if j['folder_code'] in DAILY_CODES)
    with connect(repo.path) as db:
        assert db.execute('select count(*) from business_messages').fetchone()[0] == 8
        assert db.execute('select count(*) from telegram_folder_daily_history').fetchone()[0] == 5
    assert repo.seed_new_clients(MIDNIGHT) == 0  # wait for remote clear
    assert FolderRepository(repo.path).begin_day(MIDNIGHT) == '2026-10-04'
    repo.complete_day_clear('2026-10-04')
    assert repo.begin_day(MIDNIGHT) is None
    assert repo.seed_new_clients(MIDNIGHT) == 0  # yesterday's unprocessed backlog


def test_new_day_first_message_creates_new_card_and_later_messages_keep_manager(tmp_path):
    repo = prepare(tmp_path / 'business.db')
    incoming(repo, '1001', 1, BEFORE)
    repo.seed_new_clients(BEFORE, manager_cards_chat_id=GROUP)
    api = FakeAPI()
    cards = ManagerCards(repo, GROUP, api=api)
    cards.dispatch_due(BEFORE)
    cards.handle_callback(click(1, 'OLMAS'), BEFORE)
    previous_revision = repo.assignment('1001')['revision']
    repo.begin_day(MIDNIGHT)
    repo.complete_day_clear('2026-10-04')
    incoming(repo, '1001', 2, MIDNIGHT)
    incoming(repo, '1001', 3, MIDNIGHT)
    assert repo.seed_new_clients(MIDNIGHT, manager_cards_chat_id=GROUP) == 1
    assert repo.assignment('1001')['folder_code'] == 'NEW'
    assert repo.assignment('1001')['revision'] > previous_revision
    cards.handle_callback(click(1, 'ALI', callback_id='stale'), MIDNIGHT)
    assert repo.assignment('1001')['folder_code'] == 'NEW'
    assert cards.dispatch_due(MIDNIGHT) == 1
    cards.handle_callback(click(2, 'ABBOS', callback_id='today'), MIDNIGHT)
    incoming(repo, '1001', 4, MIDNIGHT + timedelta(hours=4))
    assert repo.seed_new_clients(MIDNIGHT + timedelta(hours=4), manager_cards_chat_id=GROUP) == 0
    assert repo.assignment('1001')['folder_code'] == 'ABBOS'
    with connect(repo.path) as db:
        assert db.execute('select count(*) from telegram_manager_cards').fetchone()[0] == 2
        assert db.execute('select status from telegram_manager_cards where card_id=1').fetchone()[0] == 'cancelled'


def test_old_card_rejected_before_scheduler_notices_midnight(tmp_path):
    repo = prepare(tmp_path / 'business.db')
    incoming(repo, '1001', 1, BEFORE)
    repo.seed_new_clients(BEFORE, manager_cards_chat_id=GROUP)
    cards = ManagerCards(repo, GROUP, api=FakeAPI())
    cards.dispatch_due(BEFORE)
    cards.handle_callback(click(1, 'ALI'), MIDNIGHT)
    assert repo.assignment('1001')['folder_code'] == 'NEW'
    assert 'устарела' in cards.api.answered[-1][1]['text']


def test_restart_after_multiple_days_keeps_today_and_skips_late_yesterday(tmp_path):
    repo = prepare(tmp_path / 'business.db')
    repo.assign('1001', 'OLMAS', BEFORE)
    later = MIDNIGHT + timedelta(days=3, minutes=12)
    incoming(repo, '1001', 1, BEFORE, received=later)
    incoming(repo, '1002', 2, later)
    repo = FolderRepository(repo.path)
    assert repo.begin_day(later) == '2026-10-07'
    repo.complete_day_clear('2026-10-07')
    assert repo.seed_new_clients(later) == 1
    assert repo.assignment('1001') is None
    assert repo.assignment('1002')['folder_code'] == 'NEW'


def test_suppliers_never_get_new_or_manager_cards_next_day(tmp_path):
    repo = prepare(tmp_path / 'business.db')
    repo.assign('1001', 'SUPPLIER', BEFORE)
    with connect(repo.path) as db:
        db.execute('insert into telegram_supplier_group_members values(?,?,?)', ('1002','-1001',BEFORE.isoformat()))
    repo.begin_day(MIDNIGHT)
    repo.complete_day_clear('2026-10-04')
    incoming(repo, '1001', 1, MIDNIGHT)
    incoming(repo, '1002', 2, MIDNIGHT)
    repo.seed_new_clients(MIDNIGHT, manager_cards_chat_id=GROUP)
    assert all(r['folder_code'].startswith('SUPPLIER') for r in repo.assignments())
    with connect(repo.path) as db:
        assert db.execute('select count(*) from telegram_manager_cards').fetchone()[0] == 0


def test_gateway_clears_daily_filters_keeps_suppliers_done_and_sentinel(tmp_path):
    async def run():
        gateway = TelegramFolderGateway(settings(tmp_path / 'business.db'), client=FakeMTProtoClient())
        await gateway.connect()
        await gateway.ensure_folders()
        for index, code in enumerate((*DAILY_CODES, 'SUPPLIER', 'DONE'), 1001):
            await gateway.move(str(index), code)
        await gateway.clear_daily()
        await gateway.clear_daily()  # restart after remote success, before DB ack
        assert await gateway.snapshot() == {'1006': {'SUPPLIER'}, '1007': {'DONE'}}
        for folder in gateway.client.filters:
            assert gateway._contains(folder.include_peers, 999)
    asyncio.run(run())


def test_reset_failure_is_durable_and_blocks_stale_reconciliation(tmp_path):
    async def run():
        repo = prepare(tmp_path / 'business.db')
        repo.assign('1001', 'OLMAS', BEFORE)
        class Gateway:
            attempts = 0
            async def clear_daily(self):
                self.attempts += 1
                if self.attempts == 1:
                    error = RuntimeError('flood')
                    error.seconds = 120
                    raise error
            async def snapshot(self):
                raise AssertionError('must not reconcile stale folders before reset')
        clock = [MIDNIGHT]
        gateway = Gateway()
        service = TelegramFolderService(settings(repo.path), repository=repo, gateway=gateway, clock=lambda: clock[0])
        assert await service.run_once() == 0
        assert await service.run_once() == 0
        assert gateway.attempts == 1
        service = TelegramFolderService(settings(repo.path), repository=FolderRepository(repo.path), gateway=gateway, clock=lambda: clock[0])
        assert not await service.rollover_day()
        clock[0] += timedelta(seconds=120)
        assert await service.rollover_day()
        assert gateway.attempts == 2
        assert repo.begin_day(clock[0]) is None
    asyncio.run(run())


def test_first_install_does_not_clear_midday(tmp_path):
    repo = FolderRepository(tmp_path / 'business.db')
    repo.assign('1001', 'ALI', BEFORE)
    assert repo.begin_day(BEFORE) is None
    assert repo.assignment('1001')['folder_code'] == 'ALI'
