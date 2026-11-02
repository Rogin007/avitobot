"""Configuration: secrets, polling settings, proxy, monitored search links.

All runtime values may be overridden by environment variables / a local
``.env`` file (see ``load_config``). Heat and market thresholds live here
so the whole pipeline can be tuned without touching business logic.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

# Base URL of Avito, used both by the parser and to build the fallback
# (whole-Russia) link from a local (Yekaterinburg) one.
AVITO_BASE_URL = "https://www.avito.ru"

# Location id used by Avito for Yekaterinburg (r=6).
EKATERINBURG_LOCATION = 621540

# The "region" coordinate passed by the CDN location chooser; leaving it
# out of the config because individual search links already carry it.
_DEFAULT_LOCATION_FILTER = "621540"


@dataclass
class Config:
    # --- Telegram ---
    bot_token: str = ""
    admin_chat_id: int = 0          # where notifications are posted
    admin_id: Optional[int] = None  # optional /start owner check

    # --- Polling ---
    poll_min_seconds: int = 60
    poll_max_seconds: int = 120

    # --- Market estimation ---
    min_local_samples: int = 5       # Ekb needs >= this to trust local market
    min_rf_samples: int = 15         # otherwise gather RF-wide sample
    market_cache_ttl_seconds: int = 6 * 3600  # median cached 4-6 h
    profit_threshold: int = 5000     # profit >= this => "HOT LOT"
    price_lower_bound: int = 1000    # strip anomalies below
    price_upper_mult: float = 5.0    # strip anomalies above
    rare_threshold: int = 5          # < this matched in RF => UNCERTAIN (Step 4)
    profit_pct_denom: int = 1        # avoid div-by-zero in display

    # --- Network ---
    impersonate: str = "chrome124"
    timeout_seconds: float = 20.0
    retries: int = 3
    proxies: Optional[dict] = None   # e.g. {"http": "...", "https": "..."}

    # --- Monitored search links (Ekb by default) ---
    monitor_urls: list[str] = field(default_factory=list)

    # --- Paths ---
    db_path: str = "avito.db"
    user_agents: list[str] = field(default_factory=list)

    # --- Logging ---
    log_level: str = "DEBUG"  # DEBUG is default; override via AVITO_LOG_LEVEL

    @property
    def request_headers(self) -> dict:
        """Chrome-like headers expected by Avito's edge/CDN."""
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua": (
                '"Not/A)Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"'
            ),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
        }


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_proxy(proxies: Optional[str]) -> Optional[dict]:
    """Parse ``http://user:pass@host:port`` or ``socks5://host:port``.

    A comma-separated pair syntax (``http=...,socks5=...``) is accepted too.
    ``None`` or empty -> None.
    """
    if not proxies:
        return None
    text = proxies.strip()
    if not text:
        return None
    result: dict = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            scheme, _, url = chunk.partition("=")
            result[scheme.strip()] = url.strip()
        else:
            result["http"] = chunk
            result["https"] = chunk
    return result or None


def load_config() -> Config:
    """Build a :class:`Config` from env vars (and an optional .env file)."""
    if os.path.exists(".env"):
        _dotenv()
    cfg = Config(
        bot_token=os.getenv("AVITO_BOT_TOKEN", ""),
        admin_chat_id=_env_int("AVITO_ADMIN_CHAT_ID", 0),
        admin_id=_env_int("AVITO_ADMIN_ID", 0) or None,
        poll_min_seconds=_env_int("AVITO_POLL_MIN", 60),
        poll_max_seconds=_env_int("AVITO_POLL_MAX", 120),
        min_local_samples=_env_int("AVITO_MIN_LOCAL_SAMPLES", 5),
        min_rf_samples=_env_int("AVITO_MIN_RF_SAMPLES", 15),
        market_cache_ttl_seconds=_env_int("AVITO_CACHE_TTL", 6 * 3600),
        profit_threshold=_env_int("AVITO_PROFIT_THRESHOLD", 5000),
        price_lower_bound=_env_int("AVITO_PRICE_LOWER", 1000),
        price_upper_mult=_env_int("AVITO_PRICE_UPPER_MULT_X10", 10) / 10,
        rare_threshold=_env_int("AVITO_RARE_THRESHOLD", 5),
        impersonate=os.getenv("AVITO_IMPERSONATE", "chrome124"),
        timeout_seconds=_env_float(os.getenv("AVITO_TIMEOUT", "20.0")),
        retries=_env_int("AVITO_RETRIES", 3),
        proxies=parse_proxy(os.getenv("AVITO_PROXY")),
        db_path=os.getenv("AVITO_DB_PATH", "avito.db"),
        user_agents=_user_agents(),
        log_level=os.getenv("AVITO_LOG_LEVEL", "DEBUG").upper(),
        monitor_urls=_monitor_urls(),
    )
    return cfg


def _env_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return 20.0


def setup_logging(level: str = "DEBUG") -> None:
    """Configure root logging: a console handler, DEBUG by default.

    The level can be raised from the command line or an env var via
    :attr:`Config.log_level`. Idempotent — repeated calls are ignored.
    """
    if getattr(setup_logging, "_configured", False):
        return
    setup_logging._configured = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)


def _user_agents() -> list[str]:
    raw = os.getenv("AVITO_USER_AGENTS")
    if raw:
        return [ua.strip() for ua in raw.split("||") if ua.strip()]
    return list(DEFAULT_USER_AGENTS)


def _monitor_urls() -> list[str]:
    raw = os.getenv("AVITO_MONITOR_URLS")
    urls = [u.strip() for u in raw.split("||") if u.strip()] if raw else []
    if not urls:
        urls = [
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/microsoft-ASgBAgICA0SSAsoJsvoN3o6TAfTNFJrKjwM?cd=1&localPriority=0&q=xbox+series+s&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/microsoft-ASgBAgICA0SSAsoJsvoN3o6TAfTNFJrKjwM?localPriority=0&q=xbox+series+x&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/sony-ASgBAgICA0SSAsoJsvoN4o6TAfTNFJrKjwM?cd=1&localPriority=0&q=playstation+5&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/sony-ASgBAgICA0SSAsoJsvoN4o6TAfTNFJrKjwM?cd=1&localPriority=0&q=playstation+4&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/valve-ASgBAgICA0SSAsoJsvoNsImoEfTNFJrKjwM?localPriority=0&q=steam+deck&s=104",
            "https://www.avito.ru/ekaterinburg/noutbuki/apple-ASgBAgICAUSo5A302WY?cd=1&localPriority=0&q=macbook&s=104",
            "https://www.avito.ru/ekaterinburg/planshety_i_elektronnye_knigi/planshety-ASgBAgICAUSYAoZO?cd=1&localPriority=0&q=ipad&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igry_pristavki_i_programmy/igrovye_pristavki/nintendo-ASgBAgICA0SSAsoJsvoN4I6TAfTNFJrKjwM?cd=1&localPriority=0&q=nintendo+switch&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy/igrovye_pristavki_i_aksessuary/aksessuary/vr_ustroistva-ASgBAgICA0SSAsoJ9M0UnMqPA~CHGLD6mwM?localPriority=0&q=pico&s=104",
            "https://www.avito.ru/ekaterinburg/igry_pristavki_i_programmy?localPriority=0&q=meta+quest&s=104",
        ]
    return urls


def _dotenv(path: str = ".env") -> None:
    """Minimal dotenv loader (handles KEY=VALUE and comments)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass