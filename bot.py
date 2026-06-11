#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercari JP Sniper — bot engine
Runs as a background task alongside the Flask web dashboard.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
from aiohttp import ClientTimeout
from telegram import Bot
from telegram.error import TelegramError

from config import CONFIG

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


# ── Mercari API client ─────────────────────────────────────────────────────────
class MercariClient:
    URL = "https://api.mercari.jp/v2/entities:search"
    HEADERS = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Content-Type":    "application/json",
        "X-Platform":      "web",
        "Origin":          "https://jp.mercari.com",
        "Referer":         "https://jp.mercari.com/",
        "DPoP":            "v=1",
    }

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch(self, keyword: str, page_size: int) -> List[Dict]:
        payload = {
            "pageSize": page_size,
            "pageToken": "",
            "searchSessionId": "",
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "searchCondition": {
                "keyword":        keyword,
                "excludeKeyword": "",
                "sort":           "SORT_CREATED_TIME",
                "order":          "ORDER_DESC",
                "status":         ["STATUS_ON_SALE"],
                "itemTypes":      [],
                "sizeGroupIds":   [],
                "categoryIds":    [],
            },
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
        }

        for attempt in range(1, CONFIG["max_retries"] + 1):
            try:
                async with self.session.post(
                    self.URL,
                    json=payload,
                    headers=self.HEADERS,
                    proxy=CONFIG["proxy"],
                    timeout=ClientTimeout(total=CONFIG["timeout"]),
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        raw = data.get("items") or data.get("result", {}).get("items", [])
                        return self._parse(raw)
                    elif r.status == 429:
                        await asyncio.sleep(CONFIG["retry_delay"] * attempt)
                    else:
                        body = await r.text()
                        log.warning(f"HTTP {r.status}: {body[:200]}")
                        await asyncio.sleep(CONFIG["retry_delay"])
            except Exception as e:
                log.warning(f"Request error attempt {attempt}: {e}")
                await asyncio.sleep(CONFIG["retry_delay"])
        return []

    @staticmethod
    def _parse(raw: List[Dict]) -> List[Dict]:
        out = []
        for i in raw:
            item_id = str(i.get("id") or "")
            if not item_id:
                continue
            price = i.get("price") or i.get("sellingPrice") or 0
            try:
                price = int(price)
            except (TypeError, ValueError):
                price = 0
            thumbnail = (i.get("thumbnails") or [None])[0] or i.get("thumbnail") or ""
            out.append({
                "id":        item_id,
                "name":      i.get("name") or "—",
                "price":     price,
                "price_str": f"¥{price:,}" if price else "価格非公開",
                "url":       f"https://jp.mercari.com/item/{item_id}",
                "thumbnail": thumbnail,
                "created":   i.get("created") or 0,
                "seller":    (i.get("seller") or {}).get("name") or "",
                "condition": (i.get("itemCondition") or {}).get("name") or "",
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
