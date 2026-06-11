#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Entry point — starts the async bot loop + Flask web dashboard together.
Usage: python main.py
"""

import asyncio
import logging
import threading
from pathlib import Path

from config import CONFIG

# ── Logging setup (shared by bot + web) ───────────────────────────────────────
Path("data").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sniper")

import bot as bot_engine
from web import run_web


def main():
    log.info("=" * 60)
    log.info("  Mercari Sniper starting up")
    log.info(f"  Keyword: {CONFIG['keyword']}")
    log.info(f"  Dashboard: http://localhost:{CONFIG['web_port']}")
    log.info("=" * 60)

    # Create a dedicated asyncio event loop for the bot
    loop = asyncio.new_event_loop()

    # Start bot loop in background thread
    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    # Auto-start bot immediately
    loop.call_soon_threadsafe(bot_engine.start_bot, loop)

    # Flask runs on main thread (blocking)
    run_web(loop)


if __name__ == "__main__":
    main()
