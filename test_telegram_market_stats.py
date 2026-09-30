import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from telegram_market_stats.analyzer import MarketMessageAnalyzer, ProductModelIndex
from telegram_market_stats.collector import MarketStatsCollector
from telegram_market_stats.config import DEFAULT_COMPETITORS, MarketStatsSettings
from telegram_market_stats.repository import MarketStatsRepository


NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


def settings(tmp_path: Path, catalog_path: Path) -> MarketStatsSettings:
    return MarketStatsSettings(
        enabled=True,
        group_id=-1002188560435,
        group_title="Malika bozor N1",
        db_path=tmp_path / "market.db",
        catalog_path=catalog_path,
        poll_seconds=30,
        backfill_days=30,
        backfill_limit=1000,
        batch_size=100,
        edit_rescan_messages=0,
        competitors=dict(DEFAULT_COMPETITORS),
    )


def make_catalog(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["post_id", "product_id", "Model"])
    sheet.append(["https://t.me/example/1", "1", "iPhone 16 Pro Max"])
    sheet.append(["https://t.me/example/2", "2", "Samsung Galaxy S25 Ultra"])
    sheet.append(["https://t.me/example/3", "3", "Apple Watch Series 10"])
    workbook.save(path)


def analyzer() -> MarketMessageAnalyzer:
    return MarketMessageAnalyzer(ProductModelIndex([
        "iPhone 16 Pro Max",
        "iPhone 16 Pro",
        "Samsung Galaxy S25 Ultra",
        "Apple Watch Series 10",
    ]))


def test_analyzer_distinguishes_demand_supply_and_multiple_models():
    current = analyzer()
    demand = current.analyze(
        "Ищу iPhone 16 Pro Max 256 GB black и Samsung S25 Ultra, у кого есть?"
    )
    assert demand.intent == "demand"
    assert {item.model_name for item in demand.mentions} == {
        "iPhone 16 Pro Max", "Samsung Galaxy S25 Ultra",
    }
    iphone = next(item for item in demand.mentions if item.model_name.startswith("iPhone"))
    assert iphone.memory == "256 GB"
    assert iphone.color == "black"

    supply = current.analyze("Продам iPhone 16 Pro Max, есть в наличии")
    assert supply.intent == "supply"
    assert [item.model_name for item in supply.mentions] == ["iPhone 16 Pro Max"]


def test_analyzer_understands_short_uzbek_request():
    result = analyzer().analyze("16pm 256 qora bormi")
    assert result.intent == "demand"
    assert len(result.mentions) == 1
    assert result.mentions[0].model_name == "iPhone 16 Pro Max"
    assert result.mentions[0].memory == "256 GB"
    assert result.mentions[0].color == "black"


def test_analyzer_understands_supplier_group_shorthand_and_new_models():
    index = ProductModelIndex([
        "Apple iPhone 17",
        "Apple iPhone 17 Pro Max",
        "ZZZTex. Apple iPhone 17 Pro Max",
        "Samsung Galaxy A57",
        "Samsung Galaxy Tab S11 Ultra",
        "Apple MacBook Pro 14 (M5 10-core)",
    ])
    current = MarketMessageAnalyzer(index)
    cases = {
        "17 max 512 sim silver kere": "Apple iPhone 17 Pro Max",
        "A57 256 navy kere": "Samsung Galaxy A57",
        "S11 ultra 256 sim kere": "Samsung Galaxy Tab S11 Ultra",
        "Macbook pro 14 M5 16/512 kere": "Apple MacBook Pro 14 (M5 10-core)",
        "18 pro 256 esim blue kere": "Apple iPhone 18 Pro",
        "Airpods 5 ozi kere": "Apple AirPods 5",
        "iPad Air M3 128/256 Blue Wifi Kere": "Apple iPad Air M3",
        "18 max 512 dual glacier kere": "Apple iPhone 18 Pro Max",
        "Mi pad se 256 wifi kere": "Xiaomi Redmi Pad SE",
        "Macbook air15 m4 16/512 silver kere": "Apple MacBook Air 15 M4",
        "Oakley hstn black/clear kere": "Oakley Meta HSTN",
    }
    for message_text, expected in cases.items():
        result = current.analyze(message_text)
        assert result.intent == "demand"
        assert [mention.model_name for mention in result.mentions] == [expected]

    two_sizes = current.analyze("Macbook air 13/15 m4 256 mdn kere")
    assert [mention.model_name for mention in two_sizes.mentions] == [
        "Apple MacBook Air 13 M4",
        "Apple MacBook Air 15 M4",
    ]
    watch = current.analyze("12/46 light gold kere")
    assert watch.mentions[0].model_name == "Apple Watch Series 12"
    assert watch.mentions[0].memory == "46mm"


def test_catalog_is_loaded_from_approved_xlsx(tmp_path):
    path = tmp_path / "Bot_URLS.xlsx"
    make_catalog(path)
    index = ProductModelIndex.from_xlsx(path)
    result = index.find("Samsung S25 Ultra kerak")
    assert len(result) == 1
    assert result[0].model_name == "Samsung Galaxy S25 Ultra"


def test_real_catalog_suffixes_do_not_create_cross_brand_false_match():
    index = ProductModelIndex([
        "Apple iPhone 14 Pro",
        "Xiaomi Redmi Note 14 Pro 4G",
        "Samsung Galaxy S25 Ultra 5G",
        "Samsung Galaxy S25 Ultra 5G 2",
    ])
    redmi = index.find("Продам Redmi Note 14 Pro")
    assert [item.model_name for item in redmi] == ["Xiaomi Redmi Note 14 Pro"]
    samsung = index.find("Samsung S25 Ultra kerak")
    assert [item.model_name for item in samsung] == ["Samsung Galaxy S25 Ultra"]


def test_repository_is_separate_idempotent_and_tracks_competitor(tmp_path):
    path = tmp_path / "market.db"
    repo = MarketStatsRepository(path)
    repo.configure(-1002188560435, "Malika bozor N1", DEFAULT_COMPETITORS, NOW)
    analysis = analyzer().analyze("iPhone 16 Pro Max kerak")
    kwargs = dict(
        group_id=-1002188560435,
        message_id=100,
        sender_id=213962560,
        telegram_date=NOW,
        edited_at=None,
        text="iPhone 16 Pro Max kerak",
        analysis=analysis,
        competitor_ids=set(DEFAULT_COMPETITORS),
        processed_at=NOW,
    )
    assert repo.upsert_message(**kwargs)
    assert repo.upsert_message(**kwargs)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM market_messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM market_model_mentions").fetchone()[0] == 1
    assert repo.model_summary(days=7, now=NOW)[0]["competitor_searches"] == 1
    competitors = {row["telegram_user_id"]: row for row in repo.competitor_summary(days=7, now=NOW)}
    assert competitors["213962560"]["searches"] == 1


def test_edit_replaces_old_model_mentions(tmp_path):
    repo = MarketStatsRepository(tmp_path / "market.db")
    repo.configure(-1002188560435, "Malika bozor N1", DEFAULT_COMPETITORS, NOW)
    common = dict(
        group_id=-1002188560435,
        message_id=101,
        sender_id=999,
        telegram_date=NOW,
        competitor_ids=set(DEFAULT_COMPETITORS),
        processed_at=NOW,
    )
    repo.upsert_message(
        **common, edited_at=None, text="iPhone 16 Pro Max kerak",
        analysis=analyzer().analyze("iPhone 16 Pro Max kerak"),
    )
    repo.upsert_message(
        **common, edited_at=NOW + timedelta(minutes=1),
        text="Samsung S25 Ultra kerak",
        analysis=analyzer().analyze("Samsung S25 Ultra kerak"),
    )
    rows = repo.model_summary(days=7, now=NOW + timedelta(minutes=2))
    assert [row["model_name"] for row in rows] == ["Samsung Galaxy S25 Ultra"]


class FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.entities = []

    async def get_input_entity(self, group_id):
        self.entities.append(group_id)
        return SimpleNamespace(channel_id=abs(group_id))

    async def get_messages(self, _entity, *, limit, min_id=0, max_id=0):
        rows = [
            message for message in self.messages
            if message.id > min_id and (not max_id or message.id < max_id)
        ]
        return sorted(rows, key=lambda message: message.id, reverse=True)[:limit]


def message(message_id, text, sender_id=999, date=NOW, edit_date=None):
    return SimpleNamespace(
        id=message_id,
        message=text,
        sender_id=sender_id,
        date=date,
        edit_date=edit_date,
    )


def test_collector_persists_checkpoint_and_only_adds_new_messages(tmp_path):
    catalog = tmp_path / "Bot_URLS.xlsx"
    make_catalog(catalog)
    config = settings(tmp_path, catalog)
    repo = MarketStatsRepository(config.db_path)
    client = FakeClient([
        message(1, "обычный разговор"),
        message(2, "iPhone 16 Pro Max kerak", sender_id=6243942320),
    ])
    collector = MarketStatsCollector(
        config, repository=repo, analyzer=analyzer(), clock=lambda: NOW
    )

    async def scenario():
        await collector.start(client)
        first = await collector.collect_once()
        client.messages.append(message(3, "Samsung S25 Ultra нужен"))
        second = await collector.collect_once()
        await collector.stop()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == {"scanned": 2, "messages_with_models": 1, "demand_mentions": 1}
    assert second == {"scanned": 1, "messages_with_models": 1, "demand_mentions": 1}
    checkpoint = repo.checkpoint(config.group_id)
    assert checkpoint["last_message_id"] == 3
    assert checkpoint["initialized_at"] is not None
    assert checkpoint["backfill_complete"] == 1
    assert {row["model_name"] for row in repo.model_summary(days=7, now=NOW)} == {
        "iPhone 16 Pro Max", "Samsung Galaxy S25 Ultra",
    }


def test_large_backfill_is_resumable_across_cycles(tmp_path):
    catalog = tmp_path / "Bot_URLS.xlsx"
    make_catalog(catalog)
    base = settings(tmp_path, catalog)
    config = MarketStatsSettings(
        **{
            **{field: getattr(base, field) for field in base.__dataclass_fields__},
            "batch_size": 100,
            "backfill_limit": 250,
        }
    )
    repo = MarketStatsRepository(config.db_path)
    client = FakeClient([
        message(
            number,
            "iPhone 16 Pro Max kerak" if number % 50 == 0 else "chat",
            date=NOW - timedelta(minutes=300 - number),
        )
        for number in range(1, 301)
    ])
    collector = MarketStatsCollector(
        config, repository=repo, analyzer=analyzer(), clock=lambda: NOW
    )

    async def scenario():
        await collector.start(client)
        results = [await collector.collect_once() for _ in range(3)]
        await collector.stop()
        return results

    results = asyncio.run(scenario())
    assert [result["scanned"] for result in results] == [100, 100, 50]
    checkpoint = repo.checkpoint(config.group_id)
    assert checkpoint["backfill_scanned"] == 250
    assert checkpoint["backfill_complete"] == 1


def test_market_database_cannot_equal_business_database(tmp_path, monkeypatch):
    business = tmp_path / "business.db"
    monkeypatch.setenv("TELEGRAM_MARKET_STATS_ENABLED", "true")
    monkeypatch.setenv("BUSINESS_DB_PATH", str(business))
    monkeypatch.setenv("TELEGRAM_MARKET_STATS_DB_PATH", str(business))
    monkeypatch.setenv("TELEGRAM_MARKET_CATALOG_PATH", str(tmp_path / "catalog.xlsx"))
    try:
        MarketStatsSettings.load()
    except ValueError as exc:
        assert "separate SQLite database" in str(exc)
    else:
        raise AssertionError("shared business/market database was accepted")
