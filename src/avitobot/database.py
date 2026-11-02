"""SQLite persistence via aiosqlite.

Two responsibilities:
* dedup storage of already-sent listing ids;
* short-lived cache of computed median market prices (key:
  ``Model_Memory_Region``) so the bot does not re-hit the network each cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite  # type: ignore


@dataclass
class MarketCacheEntry:
    key: str
    median: Optional[int]
    samples: int
    scope: str                # "local" | "rf"
    region_label: str
    status: str = "ok"        # "ok" | "uncertain"
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    created_at: float = 0.0

    def is_fresh(self, ttl_seconds: int, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return now - self.created_at <= ttl_seconds


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_lots (
    lot_id   TEXT PRIMARY KEY,
    seen_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS market_cache (
    key          TEXT PRIMARY KEY,
    median       REAL,
    samples      INTEGER NOT NULL,
    scope        TEXT NOT NULL,
    region_label TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ok',
    min_price    REAL,
    max_price    REAL,
    created_at   REAL NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str = "avito.db"):
        self._path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> "Database":
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        return self

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def is_lot_seen(self, lot_id: str) -> bool:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT 1 FROM seen_lots WHERE lot_id = ?", (lot_id,)
        )
        return await cur.fetchone() is not None

    async def mark_lot_seen(self, lot_id: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO seen_lots (lot_id, seen_at) VALUES (?, ?)",
            (lot_id, time.time()),
        )
        await self._conn.commit()

    async def get_market(self, key: str) -> Optional[MarketCacheEntry]:
        """Fetch cached market entry, if any (returned even if expired —
        caller decides whether to refresh)."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT key, median, samples, scope, region_label, status, "
            "min_price, max_price, created_at "
            "FROM market_cache WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return MarketCacheEntry(
            key=row["key"],
            median=row["median"],
            samples=row["samples"],
            scope=row["scope"],
            region_label=row["region_label"],
            status=row["status"] or "ok",
            min_price=row["min_price"],
            max_price=row["max_price"],
            created_at=row["created_at"],
        )

    async def get_fresh_market(
        self, key: str, ttl_seconds: int
    ) -> Optional[MarketCacheEntry]:
        entry = await self.get_market(key)
        if entry is None or not entry.is_fresh(ttl_seconds):
            return None
        return entry

    async def set_market(
        self,
        key: str,
        *,
        median: Optional[int],
        samples: int,
        scope: str,
        region_label: str,
        status: str = "ok",
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR REPLACE INTO market_cache "
            "(key, median, samples, scope, region_label, status, "
            " min_price, max_price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, median, samples, scope, region_label, status,
             min_price, max_price, time.time()),
        )
        await self._conn.commit()

    async def prune_seen(self, keep_seconds: int = 30 * 24 * 3600) -> int:
        """Opportunistically drop stale seen-lot ids (ids are opaque)."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "DELETE FROM seen_lots WHERE seen_at < ?", (time.time() - keep_seconds,)
        )
        await self._conn.commit()
        return cur.rowcount