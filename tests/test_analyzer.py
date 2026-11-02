"""Median, anomaly cleaning and profit math."""
import pytest

from avitobot.analyzer import (
    clean_prices, median_of, estimate_profit, profit_pct, build_profit,
    ProfitFilter, MarketAnalyzer,
)
from avitobot import config as cfg_mod
from avitobot.normalizer import normalize_lot
from avitobot.parser import Listing


def _cfg():
    return cfg_mod.Config(
        profit_threshold=5000,
        price_lower_bound=1000,
        price_upper_mult=5.0,
    )


class TestCleanPrices:
    def test_lower_bound_filters(self):
        prices = [500, 10000, 10000, 10000, 999]
        cleaned = clean_prices(prices, 1000, 5.0)
        assert all(p >= 1000 for p in cleaned)

    def test_extreme_outliers_stripped(self):
        prices = [10000, 10000, 10000, 10000, 400000]
        cleaned = clean_prices(prices, 1000, 5.0)
        assert 400000 not in cleaned

    def test_all_empty_ok(self):
        assert clean_prices([], 1000, 5.0) == []


class TestMedian:
    def test_returns_clean_median(self):
        prices = [10000, 12000, 110000, 9500, 13000, 11000]
        assert median_of(prices, _cfg()) == 11000


class TestProfit:
    def test_estimate_plain(self):
        assert estimate_profit(20000, 15000) == 5000

    def test_kit_bonus_applied(self):
        assert estimate_profit(20000, 15000, kit_bonus=3000) == 8000

    def test_none_median_zero(self):
        assert estimate_profit(None, 15000) == 0

    def test_profit_pct(self):
        assert profit_pct(5000, 10000) == 50.0

    def test_hot_threshold(self):
        cfg = _cfg()
        nut = lambda profit: profit >= cfg.profit_threshold
        assert nut(estimate_profit(20000, 15000))
        assert not nut(estimate_profit(20000, 16000))


class TestBuildProfit:
    def test_build_profit_object(self):
        market = type("Market", (), {
            "median": 20000, "samples": 20, "scope": "local",
            "region_label": "Екатеринбург", "basis": "Екб", "from_cache": False,
        })()
        lot = Listing(id="1", title="ps4 pro 1тб", price=15000, url="http://x")
        norm = normalize_lot(lot.title)
        p = build_profit(lot, norm, market)
        assert p.profit == 5000


class TestRareUncertain:
    """Step 4: thin RF sample -> UNCERTAIN, no fixed profit."""

    def _market_builder(self, prices):
        cfg = _cfg()
        cfg.rare_threshold = 5
        analyzer = MarketAnalyzer(cfg, db=None, search=None)
        return analyzer._build_market(prices, "rf", "Россия")

    def test_rare_short_sample_is_uncertain(self):
        m = self._market_builder([10000, 14000])
        assert m.is_uncertain
        assert m.median is None
        assert m.samples == 2
        assert (m.min_price, m.max_price) == (10000, 14000)

    def test_sufficient_sample_is_ok(self):
        m = self._market_builder([10000, 14000, 13000, 11000, 12000, 11500])
        assert not m.is_uncertain
        assert m.median == 11750  # median of even count
        assert m.samples >= 5

    def test_empty_thin_market_uncertain_no_range(self):
        m = self._market_builder([10000])
        assert m.is_uncertain
        assert m.min_price == 10000 and m.max_price == 10000

    @staticmethod
    def _profit_stub(market):
        from avitobot.analyzer import Profit
        from avitobot.parser import Listing
        lot = Listing(id="x", title="t", price=1000, url="http://x")
        return Profit(lot=lot, market=market, profit=0, profit_pct=0.0)

    def test_profit_filter_surfaces_uncertain(self):
        cfg = _cfg()
        f = ProfitFilter(cfg)
        market = type("Market", (), {"is_uncertain": True})()
        p = self._profit_stub(market)
        assert f.should_notify(p)

    def test_profit_filter_ignores_small_profit_when_ok(self):
        cfg = _cfg()  # threshold 5000
        f = ProfitFilter(cfg)
        market = type("Market", (), {"is_uncertain": False})()
        p = self._profit_stub(market)
        assert not f.should_notify(p)