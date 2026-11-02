"""SQLite persistence: dedup + market cache TTL."""
import pytest

from avitobot.database import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


async def test_seen_dedup(db):
    assert not await db.is_lot_seen("a1")
    await db.mark_lot_seen("a1")
    assert await db.is_lot_seen("a1")
    await db.mark_lot_seen("a1")  # idempotent
    assert await db.is_lot_seen("a1")


async def test_market_cache_roundtrip(db):
    await db.set_market("ps4_pro_1024_Екб", median=20000, samples=8,
                        scope="local", region_label="Екатеринбург")
    entry = await db.get_market("ps4_pro_1024_Екб")
    assert entry is not None
    assert entry.median == 20000
    assert entry.samples == 8
    assert entry.scope == "local"


async def test_fresh_market_ttl(db):
    await db.set_market("k1", median=5, samples=2, scope="rf",
                        region_label="Россия")
    entry = await db.get_fresh_market("k1", ttl_seconds=3600)
    assert entry is not None
    stale = await db.get_fresh_market("k1", ttl_seconds=-1)
    assert stale is None  # expired

async def test_missing_market_none(db):
    assert await db.get_market("nope") is None
    assert await db.get_fresh_market("nope", 3600) is None