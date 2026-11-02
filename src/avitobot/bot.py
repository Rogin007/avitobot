"""aiogram bot: Telegram handlers + async monitoring scheduler.

Logic is split so the core (detect -> normalize -> market -> filter) is a
plain :class:`Monitor` that can be exercised without Telegram; the aiogram
part only wires it up and renders notifications.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from . import config as cfg_mod
from .analyzer import MarketAnalyzer, Profit, ProfitFilter, build_profit
from .database import Database
from .normalizer import NormalizedLot, normalize_lot
from .parser import AvitoFetcher, Listing, SearchClient, parse_listings

log = logging.getLogger(__name__)


# --- core monitoring pipeline (Telegram-agnostic, unit-testable) --------------
class Monitor:
    def __init__(
        self,
        cfg: cfg_mod.Config,
        db: Database,
        analyzer: MarketAnalyzer,
        fetcher: Optional[AvitoFetcher] = None,
    ):
        self._cfg = cfg
        self._db = db
        self._analyzer = analyzer
        self._fetcher = fetcher or AvitoFetcher(cfg)
        self._filter = ProfitFilter(cfg)
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        """Request the monitor loop to exit after the current cycle/delay."""
        self._stop_event.set()

    async def close(self) -> None:
        """Release the shared HTTP session (curl cookie jar includes it)."""
        with contextlib.suppress(Exception):
            await self._fetcher.close()

    async def detect_new(self, url: str) -> list[tuple[Listing, NormalizedLot]]:
        """Fetch a search page, normalize items, drop already-seen ids."""
        html = await self._fetcher.fetch(url)
        found: list[tuple[Listing, NormalizedLot]] = []
        for lot in parse_listings(html, cfg_mod.AVITO_BASE_URL, "local"):
            if await self._db.is_lot_seen(lot.id):
                continue
            normalized = normalize_lot(lot.title, " ".join(lot.params.values()))
            found.append((lot, normalized))
        return found

    async def run_cycle(self) -> list[Profit]:
        notifications: list[Profit] = []
        for url in self._cfg.monitor_urls:
            new = await self.detect_new(url)
            log.debug("cycle: %s -> %s new lot(s)", url, len(new))
            for lot, normalized in new:
                market = await self._analyzer.market_for(url, normalized, lot.location or "Екатеринбург")
                profit = build_profit(lot, normalized, market)
                notify = self._filter.should_notify(profit)
                await self._db.mark_lot_seen(lot.id)
                if notify:
                    notifications.append(profit)
        log.info("cycle done: %s monitored URL(s), %s notification(s)", len(self._cfg.monitor_urls), len(notifications))
        return notifications

    async def monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            cycle = asyncio.ensure_future(self.run_cycle())
            stop_waiter = asyncio.ensure_future(self._stop_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {cycle, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                if not stop_waiter.done():
                    stop_waiter.cancel()
            if cycle in done:
                try:
                    cycle.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # never kill the loop
                    log.warning("cycle failed: %s", exc)
            else:
                # Shutdown requested while a cycle is in flight.
                cycle.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cycle
                break
            if self._stop_event.is_set():
                break
            delay = random.uniform(self._cfg.poll_min_seconds, self._cfg.poll_max_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


# --- notification formatting --------------------------------------------------
def format_notification(p: Profit) -> str:
    if p.is_uncertain:
        return _format_rare(p)
    n = NormalizedLot(p.lot.title)
    header = "🔥  ВЫГОДНЫЙ ЛОТ" if p.profit >= 0 else "ЛОТ"
    lines = [
        f"<b>{header}</b>",
        "",
        f"<b>{p.lot.title}</b>",
        f"Конфигурация: <code>{n.assembly}</code>",
        "",
        f"Цена: <b>{p.lot.price:,} ₽</b>".replace(",", " "),
        f"Рынок ({p.market.basis}): <b>{p.market.median:,} ₽</b> "
        f"({p.market.samples} шт.)".replace(",", " "),
        f"Ожидаемый профит: <b>+{p.profit:,} ₽</b> ({p.profit_pct}%)".replace(",", " "),
    ]
    if p.lot.location:
        lines.append(f"Локация: {p.lot.location}")
    lines.append("")
    if p.market.from_cache:
        lines.append("(медиана из кэша)")
    return "\n".join(lines)


def _format_rare(p: Profit) -> str:
    """Step 4 UNCERTAIN plate: no fixed profit, show RF price range."""
    n = NormalizedLot(p.lot.title)
    count = p.market.samples
    lo = p.market.min_price
    hi = p.market.max_price
    lines = [
        f"<b>⚠️  РЕДКИЙ ТОВАР (в РФ найдено всего {count} шт)</b>",
        "",
        f"<b>{p.lot.title}</b>",
        f"Конфигурация: <code>{n.assembly}</code>",
        "",
    ]
    if lo is not None and hi is not None:
        lines.append(f"Диапазон цен в РФ: <b>от {lo:,} до {hi:,} ₽</b>".replace(",", " "))
    else:
        lines.append("Диапазон цен в РФ: <b>недостаточно данных</b>")
    if p.lot.location:
        lines.append(f"Локация: {p.lot.location}")
    lines.append("")
    lines.append("<i>Требуется ручная оценка.</i>")
    return "\n".join(lines)


def open_button(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть объявление", url=url)]
        ]
    )


# --- aiogram wiring -----------------------------------------------------------
class AvitoBot:
    def __init__(self, cfg: cfg_mod.Config):
        self._cfg = cfg
        self._bot = (
            Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
            if cfg.bot_token
            else None
        )
        self._dp = Dispatcher()
        self._db = Database(cfg.db_path)
        self._monitor: Optional[Monitor] = None
        self._shutdown_event = asyncio.Event()
        self._register()

    def _register(self) -> None:
        @self._dp.message(CommandStart())
        async def start(msg: Message):
            await msg.answer(
                "Авито-монитор запущен.\n"
                "Слежу за свежими объявлениями и оцениваю их ликвидность/профит."
            )

        @self._dp.message(Command("status"))
        async def status(msg: Message):
            if self._monitor is None:
                await msg.answer("нет активного монитора")
                return
            await msg.answer(
                f"URLs: {len(self._cfg.monitor_urls)}\n"
                f"Порог профита: {self._cfg.profit_threshold} ₽\n"
                f"Окно опроса: {self._cfg.poll_min_seconds}-{self._cfg.poll_max_seconds} с"
            )

    async def start(self) -> None:
        if not self._bot:
            raise RuntimeError("AVITO_BOT_TOKEN не задан")
        await self._db.connect()

        # 1) Avito monitoring first — autonomous, independently logged.
        fetcher = AvitoFetcher(self._cfg)
        search = SearchClient(fetcher, self._cfg)
        analyzer = MarketAnalyzer(self._cfg, self._db, search)
        self._monitor = Monitor(self._cfg, self._db, analyzer, fetcher)
        monitor_task = asyncio.create_task(self._monitor.monitor_loop())
        log.info("Avito monitoring started")

        # A signal (Ctrl+C / SIGTERM) may arrive at any point below: the
        # shutdown event becomes the single thing that makes every stage stop.
        shutdown_waiter = asyncio.ensure_future(self._shutdown_event.wait())
        polling_task: Optional[asyncio.Task] = None
        try:
            # Let the first Avito cycle produce logs before touching Telegram.
            warmup = asyncio.ensure_future(asyncio.sleep(3))
            await asyncio.wait({warmup, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not warmup.done():
                warmup.cancel()

            # 2) Telegram access afterwards — non-fatal and interruptible: on
            #    failure the Avito monitoring keeps running and logging.
            if not self._shutdown_event.is_set():
                polling_task = await self._connect_telegram(shutdown_waiter)

            # 3) Wait until shutdown is requested. If the Telegram poller dies
            #    on its own, keep monitoring and just wait for Ctrl+C.
            while not self._shutdown_event.is_set():
                wait_on = {shutdown_waiter}
                if polling_task is not None and not polling_task.done():
                    wait_on.add(polling_task)
                done, _ = await asyncio.wait(wait_on, return_when=asyncio.FIRST_COMPLETED)
                if polling_task in done and not shutdown_waiter.done():
                    try:
                        polling_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        log.error("Telegram poller stopped: %s; Avito monitoring continues", exc)
                    polling_task = None
                else:
                    break
        finally:
            log.info("shutdown requested, stopping...")
            if self._monitor:
                self._monitor.stop()
            await self._cancel_tasks(
                shutdown_waiter,
                monitor_task,
                *(polling_task,) if polling_task else (),
            )
            log.info("stopped gracefully")
            with contextlib.suppress(Exception):
                await self._monitor.close()

    async def _connect_telegram(
        self, shutdown_waiter: asyncio.Future
    ) -> Optional[asyncio.Task]:
        """Bring up Telegram, racing against a shutdown signal.

        Returns the polling task, or ``None`` when Telegram dropped on init
        (network trouble or a pending shutdown). Never raises into the caller.
        """

        async def _init() -> asyncio.Task:
            await self._ensure_chat_id()
            await self._bot.delete_webhook(drop_pending_updates=True)
            task = asyncio.create_task(self._dp.start_polling(self._bot))
            log.info("Telegram poller started; press Ctrl+C to stop gracefully")
            return task

        init = asyncio.ensure_future(_init())
        done, _ = await asyncio.wait({init, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if init not in done:
            # Shutdown won the race while Telegram was still starting up.
            init.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await init
            return None
        try:
            return init.result()
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            log.error("Telegram init failed; Avito monitoring keeps running: %s", exc)
            return None

    @staticmethod
    async def _cancel_tasks(*tasks: asyncio.Task) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in tasks if not t.done()), return_exceptions=True
        )

    def request_shutdown(self) -> None:
        """Signal-based hook: wakes :meth:`start` up to stop cleanly."""
        self._shutdown_event.set()

    async def _ensure_chat_id(self) -> None:
        """If admin_chat_id is unset, grab it from the first /start message.

        Works because the user pings the bot before launch; getUpdates returns
        their message, from which we read the chat id.
        """
        if self._cfg.admin_chat_id:
            return
        try:
            updates = await self._bot.get_updates(timeout=1, limit=1)
        except Exception as exc:  # network hiccup — polling may still work
            log.warning("could not fetch updates for chat_id: %s", exc)
            return
        for upd in updates:
            msg = getattr(upd, "message", None) or getattr(upd, "my_chat_member", None)
            if msg is None or not getattr(msg, "chat", None):
                continue
            self._cfg.admin_chat_id = msg.chat.id
            user = getattr(msg, "from_user", None)
            self._cfg.admin_id = getattr(user, "id", None)
            log.info("learned admin chat_id=%s", msg.chat.id)
            try:
                await self._bot.get_updates(allowed_updates=[], offset=upd.update_id + 1)
            except Exception:
                pass
            break

    async def send_profit(self, p: Profit) -> None:
        if self._bot and self._cfg.admin_chat_id:
            await self._bot.send_message(
                self._cfg.admin_chat_id,
                format_notification(p),
                reply_markup=open_button(p.lot.url),
            )

    async def stop(self) -> None:
        await self._db.close()
        if self._bot:
            await self._bot.session.close()


def main() -> None:
    cfg = cfg_mod.load_config()
    cfg_mod.setup_logging(cfg.log_level)
    if not cfg.bot_token:
        log.error("Set AVITO_BOT_TOKEN (see .env.example)")
        raise SystemExit(1)
    app = AvitoBot(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _on_signal() -> None:
        loop.call_soon_threadsafe(app.request_shutdown)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            # Windows has no add_signal_handler; fall back to signal.signal.
            signal.signal(sig, lambda *_: app.request_shutdown())

    try:
        loop.run_until_complete(app.start())
    except KeyboardInterrupt:
        loop.run_until_complete(_shutdown_fallback(app))
    finally:
        try:
            # Bound the cleanup so a stuck session close cannot hang exit.
            loop.run_until_complete(
                asyncio.wait_for(app.stop(), timeout=10)
            )
        except Exception as exc:
            log.warning("cleanup did not finish in time: %s", exc)
        finally:
            loop.close()


async def _shutdown_fallback(app: "AvitoBot") -> None:
    app.request_shutdown()
    try:
        await asyncio.wait_for(app.start(), timeout=10)
    except (asyncio.TimeoutError, RuntimeError):
        pass


if __name__ == "__main__":
    main()