"""Avito data collection via curl_cffi (Chrome TLS impersonation).

Two layers:
* :class:`AvitoFetcher` — raw HTML acquisition with UA rotation, retries,
  timeouts, optional HTTP/SOCKS5 proxy and captcha detection.
* :func:`parse_listings` — pure HTML -> list[:class:`Listing`], fully
  decoupled from HTTP so it can be unit-tested with synthetic markup.
* :class:`SearchClient` — orchestrates "Ekb first, whole-Russia fallback"
  market gathering.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs

from bs4 import BeautifulSoup

from . import config as cfg_mod

log = logging.getLogger(__name__)

# Avito bakes the item id into the canonical URL as .../item_<id>.
_ITEM_ID_RE = re.compile(r"[/_-](\d{3,})$", re.IGNORECASE)

# City segment in Ekb search links, removed for the RF-wide fallback.
CITY_SEGMENT = "ekaterinburg"

_CAPTCHA_HINTS = (
    "captcha",
    "проверка",
    "verify",
    "robot",
    "accessdenied",
    "403",
)


@dataclass
class Listing:
    id: str
    title: str
    price: int
    url: str
    description: str = ""
    params: dict = field(default_factory=dict)
    location: str = ""
    timestamp: Optional[str] = None
    scope: str = "local"   # "local" (Ekb) | "rf" (whole Russia)


class CaptchaError(Exception):
    """Raised when Avito forces a captcha/robot-check page."""


class NetworkError(Exception):
    """Transient network/timeout failure — safe to retry later."""


@dataclass
class SearchResult:
    listings: list[Listing]
    scope: str
    region_label: str


def make_rf_url(url: str) -> str:
    """Build a whole-Russia URL from an Ekb (or any city) search link.

    Strips the city path segment (``/ekaterinburg/...`` -> ``/...``) and any
    location-ish query parameters. Heuristic on purpose: callers may override.
    """
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[0] == CITY_SEGMENT:
        segments = segments[1:]
    new_path = "/" + "/".join(segments)
    query = parsed.query
    qs = parse_qs(query)
    for drop in ("l", "location", "region", "geo"):
        qs.pop(drop, None)
    new_query = "&".join(f"{k}={v[0]}" for k, v in qs.items())
    return urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params,
                       new_query, parsed.fragment))


class AvitoFetcher:
    """Async network layer with retries, UA rotation and proxies.

    A single :class:`curl_cffi AsyncSession` is reused across requests so the
    cookies Avito sets on the first successful response (ACN, uid, etc.) are
    retained — this makes the visitor look like an established human browser
    instead of a cold scraping bot. A short random pause runs before every
    request so bursts of monitored URLs don't trip the rate limiter.
    """

    def __init__(self, cfg: cfg_mod.Config):
        self._cfg = cfg
        self._ua_index = 0
        self._session: Optional["object"] = None
        self._session_lock = asyncio.Lock()

    def _next_headers(self) -> dict:
        headers = dict(self._cfg.request_headers)
        if self._cfg.user_agents:
            headers["User-Agent"] = self._cfg.user_agents[
                self._ua_index % len(self._cfg.user_agents)
            ]
            self._ua_index += 1
        return headers

    async def _request_kwargs(self) -> dict:
        kwargs: dict = {
            "headers": self._next_headers(),
            "impersonate": self._cfg.impersonate,
            "timeout": self._cfg.timeout_seconds,
            "allow_redirects": True,
        }
        if self._cfg.proxies:
            # curl_cffi accepts a single proxy; prefer https/http separately.
            proxy = self._cfg.proxies.get("https") or self._cfg.proxies.get("http")
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
        return kwargs

    async def _get_session(self):
        """Return the shared persistent session, creating it once."""
        async with self._session_lock:
            if self._session is None:
                from curl_cffi import requests
                self._session = requests.AsyncSession(
                    impersonate=self._cfg.impersonate,
                    timeout=self._cfg.timeout_seconds,
                )
            return self._session

    async def _polite_delay(self) -> None:
        """Random human-ish pause before hitting the server."""
        lo, hi = self._cfg.min_request_delay, self._cfg.max_request_delay
        if hi > 0:
            await asyncio.sleep(max(0.0, __import__("random").uniform(min(lo, hi), max(lo, hi))))

    async def fetch(self, url: str) -> str:
        """Fetch HTML with retries. Raises CaptchaError/NetworkError."""
        log.debug("GET %s", url)
        last: Optional[Exception] = None
        for attempt in range(self._cfg.retries):
            try:
                return await self._fetch_once(url)
            except CaptchaError as exc:
                # signal the caller so it can skip this cycle gracefully
                log.warning("captcha/forbidden on %s: %s", url, exc)
                raise
            except asyncio.TimeoutError as exc:
                last = exc
                log.warning("timeout fetching %s (attempt %s)", url, attempt + 1)
            except NetworkError as exc:
                last = exc
                log.warning("network error fetching %s: %s", url, exc)
            # randomized backoff (never penalizes on instant success)
            import random
            await asyncio.sleep(random.uniform(1.5, 3.5) * (attempt + 1))
        raise NetworkError(f"failed after {self._cfg.retries} retries: {url}") from last

    async def _fetch_once(self, url: str) -> str:
        from curl_cffi import requests

        await self._polite_delay()
        session = await self._get_session()
        kwargs = await self._request_kwargs()

        resp = await session.get(url, **kwargs)

        status = resp.status_code
        body = resp.text if isinstance(resp.text, str) else resp.content
        body = body if isinstance(body, bytes) else body.encode("utf-8", "ignore")
        log.debug("got HTTP %s, %s bytes from %s", status, len(body), url)
        if status == 403 or _looks_like_captcha(body):
            # after a captcha the session cookie lens is suspect: drop it so the
            # next cycle starts clean instead of inheriting a poisoned identity
            await self._reset_session()
            raise CaptchaError(f"captcha/forbidden (HTTP {status}) for {url}")
        if status >= 400:
            raise NetworkError(f"HTTP {status} for {url}")
        return resp.text if isinstance(resp.text, str) else resp.text.decode(resp.encoding or "utf-8", "ignore")

    async def _reset_session(self) -> None:
        """Close and drop the shared session (recreated on next request)."""
        async with self._session_lock:
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    log.exception("closing curl session failed")
                self._session = None

    async def close(self) -> None:
        await self._reset_session()


def _looks_like_captcha(body: bytes) -> bool:
    lowered = body.lower()
    return any(hint.encode() in lowered for hint in _CAPTCHA_HINTS[:3])


# --- pure HTML parsing --------------------------------------------------------
def _price_from_node(tag) -> Optional[int]:
    if tag is None:
        return None
    meta = tag.find("meta", attrs={"itemprop": "price"})
    if meta and meta.get("content"):
        try:
            return int(float(str(meta["content"]).replace(" ", "")))
        except ValueError:
            pass
    raw = tag.get_text(" ", strip=True)
    m = re.search(r"(\d[\d\s\u00a0\'\.]*\d)\s*([₽руб]+)", raw, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d[\d\s\u00a0\'\.]*\d)", raw)
        if not m:
            return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _param_kv(text: str) -> tuple[str, str]:
    low = text.lower()
    for sep in (":", "—", "-", "–"):
        if sep in text:
            k, _, v = text.partition(sep)
            return " ".join(k.split()), " ".join(v.split())
    return " ".join(text.split()), ""


def parse_listings(html: str, base_url: str, scope: str = "local") -> list[Listing]:
    """Extract listing cards from an Avito search-results HTML document.

    Selectors target Avito's stable ``data-marker`` attributes.
    """
    soup = BeautifulSoup(html, "lxml")
    items = soup.select('div[data-marker="item"]')
    results: list[Listing] = []
    for node in items:
        title_a = node.select_one('a[itemprop="url"], a[data-marker*="item-title"], h3[data-marker*="item-title"] > a, h3[itemprop="name"] a')
        if title_a is None:
            continue
        title = _clean(title_a.get_text(" ", strip=True)) or ""
        href = title_a.get("href") or ""
        if href.startswith("/"):
            href = urljoin(base_url, href)
        price = _price_from_node(node)

        item_id = None
        m = _ITEM_ID_RE.search(href)
        if m:
            item_id = m.group(1)
        if not item_id:
            # fall back to any marker like data-marker="item-<id>"
            marker = node.get("data-marker", "")
            m = re.search(r"item-(\d+)", marker)
            item_id = m.group(1) if m else None
        if not item_id:
            item_id = title  # degraded but still deduplicable

        params = {}
        params_el = node.select_one('[data-marker="item-specific-params"]')
        # params can be nested <li class="params-paramsList__...">
        for li in node.select('[data-marker="item-specific-params"] li'):
            k, v = _param_kv(_clean(li.get_text(" ", strip=True)))
            if k:
                params[k] = v

        desc = ""
        for container in node.select('[data-marker="item-description"], p[data-marker*="description"]'):
            desc = _clean(container.get_text(" ", strip=True))
            if desc:
                break

        location = ""
        # second (geo) item title
        geo = node.select_one('[data-marker="item-address"], span[itemprop="address"]')
        if geo is not None:
            location = _clean(geo.get_text(" ", strip=True))

        ts = None
        date_el = node.select_one('[data-marker="item-date"]')
        if date_el is not None:
            ts = _clean(date_el.get_text(" ", strip=True))

        results.append(Listing(
            id=str(item_id),
            title=title,
            price=price or 0,
            url=href,
            description=desc,
            params=params,
            location=location,
            timestamp=ts,
            scope=scope,
        ))
    return results


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class SearchClient:
    """High-level market sampling: local (Ekb) -> whole-Russia fallback."""

    def __init__(self, fetcher: AvitoFetcher, cfg: cfg_mod.Config):
        self._fetch = fetcher
        self._cfg = cfg

    async def collect(self, url: str) -> SearchResult:
        """Fetch Ekb listings; when too few, fetch RF-wide and label the scope."""
        local = await self._fetch_listings(url, "local", "Екатеринбург")
        if len(local.listings) >= self._cfg.min_local_samples:
            return local
        log.info("only %s local samples; falling back to whole-Russia", len(local.listings))
        rf_url = make_rf_url(url)
        rf = await self._fetch_listings(rf_url, "rf", "Россия")
        return rf

    async def _fetch_listings(self, url: str, scope: str, label: str) -> SearchResult:
        html = await self._fetch.fetch(url)
        listings = parse_listings(html, cfg_mod.AVITO_BASE_URL, scope)
        log.info("parsed %s listings (%s) from %s", len(listings), label, url)
        return SearchResult(listings=listings, scope=scope, region_label=label)