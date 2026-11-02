"""Dynamic market estimation and profit analysis.

* Local (Ekb) market first; falls back to a whole-Russia sample when the
  local sample is too thin.
* Computes the median after stripping anomalies.
* Caches the result in SQLite keyed by ``Model_Memory_Region`` for TTL hours.
* Estimates profit = median +/- kit adjustment - lot price.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Optional

from . import config as cfg_mod
from .database import Database
from .normalizer import NormalizedLot
from .parser import SearchClient, Listing

log = logging.getLogger(__name__)


@dataclass
class Market:
    median: Optional[int]
    samples: int
    scope: str
    region_label: str
    prices: list[int] = field(default_factory=list)
    from_cache: bool = False
    status: str = "ok"          # "ok" | "uncertain"
    min_price: Optional[int] = None
    max_price: Optional[int] = None

    @property
    def basis(self) -> str:
        return "Екб" if self.scope == "local" else "РФ"

    @property
    def is_uncertain(self) -> bool:
        return self.status == "uncertain"


@dataclass
class Profit:
    lot: Listing
    market: Market
    profit: int
    profit_pct: float

    @property
    def is_hot(self) -> bool:
        return self.profit >= 0

    @property
    def is_uncertain(self) -> bool:
        return self.market.is_uncertain


def _median(prices: list[int]) -> Optional[int]:
    if not prices:
        return None
    return int(statistics.median(prices))


def clean_prices(prices: list[int], lower: int, upper_mult: float) -> list[int]:
    """Drop anomalies: absurdly low (typos, deposits) and extreme outliers."""
    if not prices:
        return []
    out: list[int] = []
    for p in prices:
        if p < lower:
            continue
        out.append(p)
    if len(out) < 2:
        return out
    median = statistics.median(out)
    upper = median * upper_mult
    return [p for p in out if p <= upper]


def median_of(prices: list[int], cfg: cfg_mod.Config) -> Optional[int]:
    cleaned = clean_prices(prices, cfg.price_lower_bound, cfg.price_upper_mult)
    return _median(cleaned)


def estimate_profit(median: Optional[int], price: int, kit_bonus: int = 0) -> int:
    """profit = median (+kit) - lot price. ``None`` median -> 0 (unknown)."""
    if median is None:
        return 0
    return median + kit_bonus - price


class MarketAnalyzer:
    def __init__(self, cfg: cfg_mod.Config, db: Database, search: SearchClient):
        self._cfg = cfg
        self._db = db
        self._search = search

    async def market_for(
        self, url: str, lot: NormalizedLot, region_label: str = "Екатеринбург"
    ) -> Market:
        """Return a (possibly cached) Market for the lot's config group."""
        model_key = lot.market_key if lot.market_key != "unknown::" else "unknown"
        cache_key = f"{model_key}_{region_label}"
        cached = await self._db.get_fresh_market(
            cache_key, self._cfg.market_cache_ttl_seconds
        )
        if cached is not None:
            log.debug("market cache hit for %s", cache_key)
            return Market(
                median=cached.median,
                samples=cached.samples,
                scope=cached.scope,
                region_label=cached.region_label,
                from_cache=True,
                status=cached.status,
                min_price=cached.min_price,
                max_price=cached.max_price,
            )

        result = await self._search.collect(url)
        prices = [l.price for l in result.listings if l.price > 0]
        market = self._build_market(prices, result.scope, result.region_label)
        await self._db.set_market(
            cache_key,
            median=market.median,
            samples=market.samples,
            scope=market.scope,
            region_label=market.region_label,
            status=market.status,
            min_price=market.min_price,
            max_price=market.max_price,
        )
        return market

    def _build_market(
        self, prices: list[int], scope: str, region_label: str
    ) -> Market:
        """Assemble market from the sampled prices.

        Step 4: when fewer than ``rare_threshold`` matched lots are available,
        the market is marked ``uncertain`` and only a price range is reported
        (no fixed median -> no fixed profit).
        """
        positive = [p for p in prices if p > 0]

        # Strict sample: anomalies stripped, used when we have enough data.
        strict = clean_prices(
            positive, self._cfg.price_lower_bound, self._cfg.price_upper_mult
        )

        # Relaxation (Step 4a): we never filtered by kit at query time, so the
        # "model + memory only" broader sample is simply *all* positive prices
        # returned by the (already model-specific) search URL.
        matched = positive

        rare = len(matched) < self._cfg.rare_threshold

        if rare:
            lo = min(matched) if matched else None
            hi = max(matched) if matched else None
            return Market(
                median=None,                 # no reliable fixed profit
                samples=len(matched),
                scope=scope,
                region_label=region_label,
                prices=matched,
                status="uncertain",
                min_price=lo,
                max_price=hi,
            )

        median = _median(strict)
        return Market(
            median=median,
            samples=len(strict),
            scope=scope,
            region_label=region_label,
            prices=strict,
            status="ok",
            min_price=min(strict) if strict else None,
            max_price=max(strict) if strict else None,
        )


def profit_pct(profit: int, price: int) -> float:
    if price <= 0:
        return 0.0
    return round(profit / price * 100, 1)


def build_profit(lot: Listing, normalized: NormalizedLot, market: Market) -> Profit:
    # kit_bonus is a signed net adjustment: +N for a rich kit, -N for a bare one.
    kit = normalized.kit_bonus
    profit = estimate_profit(market.median, lot.price, kit)
    return Profit(
        lot=lot,
        market=market,
        profit=profit,
        profit_pct=profit_pct(profit, lot.price),
    )


class ProfitFilter:
    """Decides which lots to surface: hot (profit >= threshold) or rare
    (market uncertain -> needs manual review, no fixed profit)."""

    def __init__(self, cfg: cfg_mod.Config):
        self._threshold = cfg.profit_threshold

    def should_notify(self, p: Profit) -> bool:
        if p.is_uncertain:
            return True
        return p.profit >= self._threshold