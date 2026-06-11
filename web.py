#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercari Sniper — Web Dashboard
Flask app that runs alongside the bot loop.
"""

import asyncio
import json
import logging
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, jsonify, request, redirect, url_for, session

import bot as bot_engine
from config import CONFIG

app = Flask(__name__)
app.secret_key = "mercari-sniper-secret-key-change-me"

log = logging.getLogger("sniper.web")

# ── Auth ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == CONFIG["web_password"]:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages ──────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ── API endpoints ──────────────────────────────────────────────────────────────
@app.route("/api/state")
@login_required
def api_state():
    return jsonify(bot_engine.state)


@app.route("/api/listings")
@login_required
def api_listings():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    search   = request.args.get("q", "").lower()
    all_items = bot_engine.listings.all()
    if search:
        all_items = [i for i in all_items if search in i["name"].lower()]
    start  = (page - 1) * per_page
    end    = start + per_page
    return jsonify({
        "items": all_items[start:end],
        "total": len(all_items),
        "page":  page,
        "pages": max(1, (len(all_items) + per_page - 1) // per_page),
    })


@app.route("/api/logs")
@login_required
def api_logs():
    log_path = Path(CONFIG["log_file"])
    if not log_path.exists():
        return jsonify({"lines": []})
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return jsonify({"lines": lines[-200:]})   # last 200 lines


@app.route("/api/control", methods=["POST"])
@login_required
def api_control():
    action = request.json.get("action")
    loop   = app.config["BOT_LOOP"]

    if action == "start":
        if not bot_engine.state["running"]:
            loop.call_soon_threadsafe(bot_engine.start_bot, loop)
        return jsonify({"ok": True, "status": "starting"})

    elif action == "stop":
        bot_engine.stop_bot()
        return jsonify({"ok": True, "status": "stopping"})

    elif action == "pause":
        bot_engine.pause_bot()
        return jsonify({"ok": True, "status": "paused"})

    elif action == "resume":
        bot_engine.resume_bot()
        return jsonify({"ok": True, "status": "running"})

    elif action == "set_keyword":
        kw = request.json.get("keyword", "").strip()
        if kw:
            bot_engine.state["keyword"] = kw
            return jsonify({"ok": True, "keyword": kw})
        return jsonify({"ok": False, "error": "Empty keyword"}), 400

    elif action == "set_interval":
        try:
            iv = int(request.json.get("interval", 8))
            iv = max(5, min(iv, 120))
            bot_engine.state["poll_interval"] = iv
            return jsonify({"ok": True, "interval": iv})
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid interval"}), 400

    return jsonify({"ok": False, "error": "Unknown action"}), 400


# ── Run ────────────────────────────────────────────────────────────────────────
def run_web(loop: asyncio.AbstractEventLoop):
    app.config["BOT_LOOP"] = loop
    app.run(
        host=CONFIG["web_host"],
        port=CONFIG["web_port"],
        debug=False,
        use_reloader=False,
    )
