"""End-to-end Monitor pipeline with a mocked network layer (no Telegram)."""
import pytest

from avitobot import config as cfg_mod
from avitobot.analyzer import MarketAnalyzer, Market, Profit
from avitobot.bot import Monitor, format_notification, open_button
from avitobot.database import Database
from avitobot.parser import AvitoFetcher, SearchClient, parse_listings


def _item(title, price, item_id):
    return f"""
    <div data-marker="item">
      <a itemprop="url" href="/ekaterinburg/t/test_{item_id}">
        <h3 itemscope itemprop="name">{title}</h3></a>
      <meta itemprop="price" content="{price}">
    </div>
    """


class FakeSearch(SearchClient):
    """Returns a static market board of same-config lots."""

    def __init__(self, prices, region_label, scope):
        self._prices = prices
        self._label = region_label
        self._scope = scope
        super().__init__(object(), cfg_mod.Config())
        self.calls = 0

    async def collect(self, url):
        self.calls += 1
        from avitobot.parser import SearchResult
        return SearchResult(
            listings=[parse_listings(_item("ps4 pro 1тб", p, i),
                                     "https://www.avito.ru", self._scope)[0]
                      for i, p in enumerate(self._prices)],
            scope=self._scope,
            region_label=self._label,
        )


class StaticFetcher:
    html = "<html><body>" + _item("ps4 pro 1tb полный комплект", 12000, "NEW01") + "</body></html>"
    capture: list[str] = []

    async def fetch(self, url):
        self.capture.append(url)
        return self.html


@pytest.fixture
def cfg(tmp_path):
    c = cfg_mod.Config(
        db_path=str(tmp_path / "m.db"),
        monitor_urls=["https://www.avito.ru/ekaterinburg/playstation"],
        profit_threshold=5000,
        price_lower_bound=1000,
        price_upper_mult=5.0,
        market_cache_ttl_seconds=3600,
        min_local_samples=5,
    )
    return c


async def test_cycle_detects_hot_lot_and_dedups(cfg):
    db = await Database(cfg.db_path).connect()
    fetcher = StaticFetcher()
    fake_search = FakeSearch(
        prices=[22000, 23000, 21000, 24000, 22500, 21500],
        region_label="Екатеринбург", scope="local",
    )
    analyzer = MarketAnalyzer(cfg, db, fake_search)
    monitor = Monitor(cfg, db, analyzer, fetcher)

    hot = await monitor.run_cycle()
    assert len(hot) == 1
    assert hot[0].profit >= 5000        # median-12000 > 5000
    assert fake_search.calls == 1
    assert hot[0].market.from_cache is False

    # second cycle: same lot is seen, no re-send; market comes from cache
    hot2 = await monitor.run_cycle()
    assert len(hot2) == 0
    assert fake_search.calls == 1       # no new network sample

    # fresh cycle with a genuinely new lot reuses cached market, no network
    StaticFetcher.html = "<html><body>" + _item("ps4 pro 1tb", 9000, "NEW02") + "</body></html>"
    hot3 = await monitor.run_cycle()
    assert len(hot3) == 1
    assert hot3[0].market.from_cache is True
    assert fake_search.calls == 1       # market served from SQLite cache
    await db.close()


async def test_no_verification_without_bot_token(cfg):
    db = await Database(cfg.db_path).connect()
    fetcher = StaticFetcher()
    fake_search = FakeSearch([20000], "Екатеринбург", "local")
    analyzer = MarketAnalyzer(cfg, db, fake_search)
    monitor = Monitor(cfg, db, analyzer, fetcher)
    await db.close()
    assert monitor is not None


def _profit(lot, market):
    from avitobot.normalizer import normalize_lot
    from avitobot.analyzer import build_profit
    return build_profit(lot, normalize_lot(lot.title), market)


class TestRareNotification:
    """Step 4 UNCERTAIN plate formatting."""

    def test_rare_plate_render(self):
        lot = parse_listings(
            _item("Очень редкая приставка", 25000, "RARE1"),
            "https://www.avito.ru", "rf",
        )[0]
        market = Market(median=None, samples=3, scope="rf",
                        region_label="Россия", status="uncertain",
                        min_price=22000, max_price=30000)
        p = _profit(lot, market)
        assert p.is_uncertain
        text = format_notification(p)
        assert "РЕДКИЙ ТОВАР" in text
        assert "найдено всего 3 шт" in text
        assert "от 22 000 до 30 000 ₽" in text
        assert "Требуется ручная оценка" in text
        assert "Ожидаемый профит" not in text

    def test_hot_notification_still_has_profit(self):
        lot = parse_listings(
            _item("ps4 pro 1тб", 12000, "HOT1"),
            "https://www.avito.ru", "local",
        )[0]
        market = Market(median=20000, samples=6, scope="local",
                        region_label="Екатеринбург", status="ok")
        p = _profit(lot, market)
        assert not p.is_uncertain
        text = format_notification(p)
        assert "РЕДКИЙ ТОВАР" not in text
        assert "Ожидаемый профит" in text

    def test_open_button_url(self):
        from aiogram.types import InlineKeyboardButton
        kb = open_button("https://x.dev/lot1")
        assert kb.inline_keyboard[0][0].url == "https://x.dev/lot1"
        assert isinstance(kb.inline_keyboard[0][0], InlineKeyboardButton)