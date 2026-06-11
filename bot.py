#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercari JP Sniper — bot engine
Runs as a background task alongside the Flask web dashboard.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
import requests
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

from config import CONFIG, PROXY_LIST

log = logging.getLogger("sniper.bot")


# ── Shared state (read by web dashboard) ──────────────────────────────────────
state = {
    "running":        False,
    "paused":         False,
    "status":         "stopped",   # stopped | running | paused | error
    "keyword":        CONFIG["keyword"],
    "poll_interval":  CONFIG["poll_interval"],
    "total_sent":     0,
    "total_polls":    0,
    "last_poll":      None,
    "last_error":     None,
    "started_at":     None,
}


# ── Persistent seen-IDs ────────────────────────────────────────────────────────
class SeenIDs:
    def __init__(self):
        self.path = Path(CONFIG["seen_ids_file"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ids: Set[str] = set()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.ids = set(json.loads(self.path.read_text()))
            except Exception:
                self.ids = set()

    def save(self):
        self.path.write_text(json.dumps(list(self.ids)))

    def seen(self, i: str) -> bool:
        return i in self.ids

    def mark(self, i: str):
        self.ids.add(i)


# ── Listings store (shown in dashboard) ───────────────────────────────────────
class ListingsStore:
    def __init__(self):
        self.path = Path(CONFIG["listings_file"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: List[Dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
            except Exception:
                self.items = []

    def save(self):
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=2))

    def add(self, item: Dict):
        # Prepend newest first
        self.items.insert(0, {**item, "found_at": datetime.utcnow().isoformat()})
        # Trim to max
        self.items = self.items[: CONFIG["max_stored_listings"]]
        self.save()

    def all(self) -> List[Dict]:
        return self.items


# ── Mercari web scraper ────────────────────────────────────────────────────────
class MercariClient:
    """
    Scrapes jp.mercari.com search results instead of calling the private API
    (which requires authentication and returns 401 for unauthenticated requests).

    Strategy
    --------
    Mercari's search page is a Next.js SSR app.  The full page data is embedded
    in a <script id="__NEXT_DATA__"> JSON blob — we parse that first because it
    is structured and reliable.  If that blob is absent (e.g. a JS-only render
    fallback), we fall back to scraping <li> / <article> elements with
    BeautifulSoup.
    """

    SEARCH_URL = (
        "https://jp.mercari.com/search"
        "?keyword={keyword}"
        "&sort_order=created_time"
        "&order=desc"
        "&status=on_sale"
    )
    HEADERS = {
        "User-Agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://jp.mercari.com/",
        "DNT":             "1",
    }

    def __init__(self, session: aiohttp.ClientSession):
        # aiohttp session kept for interface compatibility; HTTP is done via
        # requests so BeautifulSoup can work synchronously.
        self.session = session

    def _next_proxy(self) -> Optional[str]:
        """Return the next proxy in round-robin order, or None if list is empty."""
        if not PROXY_LIST:
            return None
        proxy = PROXY_LIST[CONFIG["proxy_index"] % len(PROXY_LIST)]
        CONFIG["proxy_index"] = (CONFIG["proxy_index"] + 1) % len(PROXY_LIST)
        return proxy

    def _proxy_dict(self, proxy_url: Optional[str]) -> Optional[dict]:
        """Convert a proxy URL string to the dict format requests expects."""
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def _fetch_html(self, keyword: str) -> Optional[str]:
        """
        Synchronous HTML fetch with proxy rotation and retry logic.
        Returns the raw HTML string on success, or None after all retries fail.
        """
        url = self.SEARCH_URL.format(keyword=requests.utils.quote(keyword))
        max_attempts = CONFIG["max_retries"] + max(len(PROXY_LIST), 1)

        for attempt in range(1, max_attempts + 1):
            proxy = self._next_proxy()
            proxies = self._proxy_dict(proxy)
            log.debug(f"Scrape attempt {attempt} via proxy {proxy}")
            try:
                resp = requests.get(
                    url,
                    headers=self.HEADERS,
                    proxies=proxies,
                    timeout=CONFIG["timeout"],
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    log.debug(f"Got {len(resp.text)} bytes from {proxy}")
                    return resp.text
                elif resp.status_code == 429:
                    log.warning(f"429 rate-limit via {proxy}, backing off")
                    time.sleep(CONFIG["retry_delay"] * attempt)
                else:
                    log.warning(
                        f"HTTP {resp.status_code} via {proxy}: "
                        f"{resp.text[:200]}"
                    )
                    time.sleep(CONFIG["retry_delay"])
            except requests.exceptions.ProxyError as e:
                log.warning(f"Proxy {proxy} error (attempt {attempt}): {e}")
                time.sleep(CONFIG["retry_delay"])
            except requests.exceptions.Timeout:
                log.warning(f"Timeout via {proxy} (attempt {attempt})")
                time.sleep(CONFIG["retry_delay"])
            except Exception as e:
                log.warning(f"Request error attempt {attempt} via {proxy}: {e}")
                time.sleep(CONFIG["retry_delay"])

        log.error("All scrape attempts exhausted — returning empty list")
        return None

    async def fetch(self, keyword: str, page_size: int) -> List[Dict]:
        """
        Async entry point called by the bot loop.
        Runs the blocking HTTP + parse work in a thread-pool executor so the
        event loop is not blocked.
        """
        loop = asyncio.get_event_loop()
        html = await loop.run_in_executor(None, self._fetch_html, keyword)
        if not html:
            return []
        items = await loop.run_in_executor(None, self._parse_html, html)
        return items[:page_size]

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_html(self, html: str) -> List[Dict]:
        """
        Try the fast __NEXT_DATA__ path first; fall back to HTML scraping.
        """
        items = self._parse_next_data(html)
        if items:
            log.debug(f"Parsed {len(items)} items from __NEXT_DATA__")
            return items

        log.debug("__NEXT_DATA__ parse yielded nothing — trying HTML scrape")
        items = self._parse_html_tags(html)
        log.debug(f"HTML scrape yielded {len(items)} items")
        return items

    def _parse_next_data(self, html: str) -> List[Dict]:
        """
        Mercari's Next.js pages embed all SSR data in:
            <script id="__NEXT_DATA__" type="application/json">{ … }</script>
        We extract that blob and walk the known key paths to find item lists.
        """
        try:
            soup = BeautifulSoup(html, "lxml")
            tag = soup.find("script", {"id": "__NEXT_DATA__"})
            if not tag or not tag.string:
                return []

            data = json.loads(tag.string)

            # Walk common paths where Mercari stores search results
            candidates: List[dict] = []

            # Path 1: props.pageProps.initialState.search.items
            try:
                candidates = (
                    data["props"]["pageProps"]["initialState"]["search"]["items"]
                )
            except (KeyError, TypeError):
                pass

            # Path 2: props.pageProps.items
            if not candidates:
                try:
                    candidates = data["props"]["pageProps"]["items"]
                except (KeyError, TypeError):
                    pass

            # Path 3: props.pageProps.searchResult.items
            if not candidates:
                try:
                    candidates = (
                        data["props"]["pageProps"]["searchResult"]["items"]
                    )
                except (KeyError, TypeError):
                    pass

            # Path 4: deep-search for any list keyed "items" that contains
            # dicts with an "id" field (last-resort recursive scan)
            if not candidates:
                candidates = self._deep_find_items(data)

            return self._normalise(candidates)

        except Exception as e:
            log.debug(f"__NEXT_DATA__ parse error: {e}")
            return []

    def _deep_find_items(self, obj, depth: int = 0) -> List[dict]:
        """
        Recursively search a JSON structure for a list of item dicts.
        Stops at depth 10 to avoid runaway recursion on large blobs.
        """
        if depth > 10:
            return []
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and "id" in obj[0]:
                return obj
            for v in obj:
                result = self._deep_find_items(v, depth + 1)
                if result:
                    return result
        elif isinstance(obj, dict):
            if "items" in obj and isinstance(obj["items"], list):
                result = self._deep_find_items(obj["items"], depth + 1)
                if result:
                    return result
            for v in obj.values():
                result = self._deep_find_items(v, depth + 1)
                if result:
                    return result
        return []

    def _parse_html_tags(self, html: str) -> List[Dict]:
        """
        Fallback: scrape visible listing cards from the HTML.
        Mercari renders each result as an <li> or <article> containing an <a>
        whose href is /item/<id>.  We extract what we can from the markup.
        """
        try:
            soup = BeautifulSoup(html, "lxml")
            out: List[Dict] = []

            # Find all anchor tags pointing to item pages
            item_links = soup.find_all(
                "a", href=re.compile(r"/item/m\w+")
            )
            seen_ids: set = set()

            for a in item_links:
                href = a.get("href", "")
                m = re.search(r"/item/(m\w+)", href)
                if not m:
                    continue
                item_id = m.group(1)
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                # Name: try alt text on the thumbnail image, then aria-label
                img = a.find("img")
                name = ""
                if img:
                    name = img.get("alt", "")
                if not name:
                    name = a.get("aria-label", "") or a.get_text(strip=True) or "—"

                # Price: look for ¥ pattern in the card text
                card_text = a.get_text(" ", strip=True)
                price = 0
                price_m = re.search(r"[¥￥][\s]?([\d,]+)", card_text)
                if price_m:
                    try:
                        price = int(price_m.group(1).replace(",", ""))
                    except ValueError:
                        price = 0

                # Thumbnail
                thumbnail = ""
                if img:
                    thumbnail = img.get("src") or img.get("data-src") or ""

                out.append({
                    "id":        item_id,
                    "name":      name or "—",
                    "price":     price,
                    "price_str": f"¥{price:,}" if price else "価格非公開",
                    "url":       f"https://jp.mercari.com/item/{item_id}",
                    "thumbnail": thumbnail,
                    "created":   0,
                    "seller":    "",
                    "condition": "",
                })

            return out

        except Exception as e:
            log.debug(f"HTML tag scrape error: {e}")
            return []

    @staticmethod
    def _normalise(raw: List[dict]) -> List[Dict]:
        """
        Convert raw item dicts (from __NEXT_DATA__ or API shape) into the
        canonical format used by the rest of the bot.
        """
        out = []
        for i in raw:
            item_id = str(i.get("id") or "")
            if not item_id:
                continue

            # Price may be nested or flat
            price = (
                i.get("price")
                or i.get("sellingPrice")
                or (i.get("pricingInfo") or {}).get("price")
                or 0
            )
            try:
                price = int(price)
            except (TypeError, ValueError):
                price = 0

            # Thumbnail may be a list or a single string
            thumbnails = i.get("thumbnails") or []
            thumbnail = (
                thumbnails[0] if thumbnails
                else i.get("thumbnail") or i.get("thumbnailUrl") or ""
            )

            # Seller name
            seller = (
                (i.get("seller") or {}).get("name")
                or i.get("sellerName")
                or ""
            )

            # Item condition
            condition = (
                (i.get("itemCondition") or {}).get("name")
                or i.get("conditionName")
                or ""
            )

            out.append({
                "id":        item_id,
                "name":      i.get("name") or "—",
                "price":     price,
                "price_str": f"¥{price:,}" if price else "価格非公開",
                "url":       f"https://jp.mercari.com/item/{item_id}",
                "thumbnail": thumbnail,
                "created":   i.get("created") or i.get("createdTime") or 0,
                "seller":    seller,
                "condition": condition,
            })

        out.sort(key=lambda x: x["created"], reverse=True)
        return out


# ── Telegram sender ────────────────────────────────────────────────────────────
class Sender:
    def __init__(self):
        self.bot = Bot(token=CONFIG["telegram_token"])
        self._sem = asyncio.Semaphore(3)

    async def verify(self) -> bool:
        try:
            me = await self.bot.get_me()
            log.info(f"Telegram OK: @{me.username}")
            return True
        except TelegramError as e:
            log.error(f"Telegram error: {e}")
            return False

    async def send(self, item: Dict) -> bool:
        async with self._sem:
            def esc(s: str) -> str:
                for c in r"_*[]()~`>#+-=|{}.!\\":
                    s = s.replace(c, f"\\{c}")
                return s

            lines = [f"🆕 *{esc(item['name'])}*", "", f"💰 {esc(item['price_str'])}"]
            if item.get("condition"):
                lines.append(f"📦 {esc(item['condition'])}")
            if item.get("seller"):
                lines.append(f"👤 {esc(item['seller'])}")
            lines += ["", f"[→ View listing]({item['url']})"]
            msg = "\n".join(lines)

            try:
                if item.get("thumbnail"):
                    await self.bot.send_photo(
                        chat_id=CONFIG["telegram_channel"],
                        photo=item["thumbnail"],
                        caption=msg,
                        parse_mode="MarkdownV2",
                    )
                else:
                    await self.bot.send_message(
                        chat_id=CONFIG["telegram_channel"],
                        text=msg,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=False,
                    )
                return True
            except TelegramError:
                try:
                    plain = f"🆕 {item['name']}\n💰 {item['price_str']}\n{item['url']}"
                    await self.bot.send_message(
                        chat_id=CONFIG["telegram_channel"],
                        text=plain,
                        disable_web_page_preview=False,
                    )
                    return True
                except TelegramError as e:
                    log.error(f"Send failed: {e}")
                    return False


# ── Main sniper engine ─────────────────────────────────────────────────────────
seen      = SeenIDs()
listings  = ListingsStore()
sender    = Sender()
_session: Optional[aiohttp.ClientSession] = None
_mercari: Optional[MercariClient] = None
_save_ctr = 0


async def _get_session() -> aiohttp.ClientSession:
    global _session, _mercari
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10),
            timeout=ClientTimeout(total=30),
        )
        _mercari = MercariClient(_session)
    return _session


async def _poll():
    global _save_ctr
    await _get_session()
    items = await _mercari.fetch(state["keyword"], CONFIG["page_size"])
    state["total_polls"] += 1
    state["last_poll"] = datetime.utcnow().isoformat()

    new = [i for i in items if not seen.seen(i["id"])]
    if not new:
        return

    log.info(f"🔥 {len(new)} new listing(s)")
    for item in reversed(new):
        ok = await sender.send(item)
        if ok:
            seen.mark(item["id"])
            listings.add(item)
            state["total_sent"] += 1
            _save_ctr += 1
            if _save_ctr >= CONFIG["save_every"]:
                seen.save()
                _save_ctr = 0
            log.info(f"  ✓ {item['name'][:55]} — {item['price_str']}")
        await asyncio.sleep(0.4)


async def run_loop():
    global _session, _mercari
    state["running"] = True
    state["status"]  = "running"
    state["started_at"] = datetime.utcnow().isoformat()
    log.info("Bot loop started")

    await _get_session()
    tg_ok = await sender.verify()
    if not tg_ok:
        state["status"] = "error"
        state["last_error"] = "Telegram token/channel invalid"
        return

    # Seed on first run
    if not seen.ids:
        log.info("First run — seeding existing listings...")
        items = await _mercari.fetch(state["keyword"], CONFIG["page_size"])
        for i in items:
            seen.mark(i["id"])
        seen.save()
        log.info(f"Seeded {len(seen.ids)} IDs")

    while state["running"]:
        if not state["paused"]:
            try:
                await _poll()
            except Exception as e:
                log.error(f"Poll error: {e}", exc_info=True)
                state["last_error"] = str(e)
        await asyncio.sleep(state["poll_interval"])

    if _session and not _session.closed:
        await _session.close()
    state["status"] = "stopped"
    log.info("Bot loop stopped")


def start_bot(loop: asyncio.AbstractEventLoop):
    """Called from web app to start bot in background."""
    if not state["running"]:
        loop.create_task(run_loop())


def stop_bot():
    state["running"] = False
    state["paused"]  = False


def pause_bot():
    state["paused"] = True
    state["status"] = "paused"


def resume_bot():
    state["paused"] = False
    state["status"] = "running"
