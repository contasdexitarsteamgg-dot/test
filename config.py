CONFIG = {
    # ── Telegram ───────────────────────────────────────────────────────────
    "telegram_token":   "8860945087:AAGs7XqwhiMSzV5das8Jmwwxn1K-6bhcPC4",
    "telegram_channel": "-5018607911",

    # ── Search ─────────────────────────────────────────────────────────────
    "keyword":       "UNDERCOVER",
    "poll_interval": 8,
    "page_size":     30,

    # ── Storage ────────────────────────────────────────────────────────────
    "seen_ids_file":         "data/seen_ids.json",
    "listings_file":         "data/listings.json",
    "log_file":              "data/sniper.log",
    "max_stored_listings":   500,

    # ── Web dashboard ──────────────────────────────────────────────────────
    "web_host":     "0.0.0.0",
    "web_port":     5000,
    "web_password": "admin",   # ← change this

    # ── Proxy ──────────────────────────────────────────────────────────────
    "proxy": None,

    # ── HTTP ───────────────────────────────────────────────────────────────
    "timeout":     12,
    "max_retries":  3,
    "retry_delay":  2,
    "save_every":  20,
}
