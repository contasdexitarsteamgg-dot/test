import os

# Parse MERCARI_PROXIES env var (comma-separated list of proxy URLs).
# Falls back to the hardcoded default list when the variable is not set.
_DEFAULT_PROXIES = [
    "http://34.84.162.206:38080",
    "http://133.232.90.85:80",
    "http://116.80.50.232:3172",
    "http://45.32.53.102:80",
    "http://45.32.53.102:443",
    "http://116.80.66.25:3172",
    "http://116.80.96.250:3172",
    "http://116.80.64.184:3172",
    "http://116.80.48.136:3172",
]

_raw_proxies = os.environ.get("MERCARI_PROXIES", "")
PROXY_LIST: list = (
    [p.strip() for p in _raw_proxies.split(",") if p.strip()]
    if _raw_proxies.strip()
    else _DEFAULT_PROXIES
)

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
    # Legacy single-proxy key kept for compatibility; rotation is handled via
    # PROXY_LIST and the proxy_index counter below.
    "proxy":       None,
    "proxy_index": 0,          # round-robin cursor, mutated by MercariClient

    # ── HTTP ───────────────────────────────────────────────────────────────
    "timeout":     12,
    "max_retries":  3,
    "retry_delay":  2,
    "save_every":  20,
}

