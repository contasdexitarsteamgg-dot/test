# Mercari Sniper

Monitors jp.mercari.com for new listings matching a keyword and posts them to Telegram instantly. Includes a web dashboard to control the bot and view listings.

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Edit config.py
Your credentials are already set. Just change the `web_password`:
```python
"web_password": "yourpassword",
```

### 3. Run
```bash
python main.py
```

Open **http://localhost:5000** in your browser. Default password: `admin`

---

## ⚠️ Japanese IP required
Mercari JP blocks non-Japanese IPs. Options:
- Run this on a **Japanese VPS** (e.g. Vultr Tokyo, ~$5/mo)
- Set `"proxy"` in config.py to a Japanese proxy URL

---

## Running as a background service (Linux/VPS)

```bash
# Install screen
apt install screen -y

# Start in background
screen -S sniper
python main.py
# Ctrl+A then D to detach

# Reattach anytime
screen -r sniper
```

Or as a systemd service:
```ini
# /etc/systemd/system/sniper.service
[Unit]
Description=Mercari Sniper
After=network.target

[Service]
WorkingDirectory=/path/to/mercari-sniper
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable sniper && sudo systemctl start sniper
```

---

## Files
| File | Purpose |
|---|---|
| `main.py` | Entry point — starts bot + web dashboard |
| `bot.py` | Bot engine (Mercari polling + Telegram sending) |
| `web.py` | Flask web dashboard |
| `config.py` | All configuration |
| `data/seen_ids.json` | Tracks sent listings (don't delete) |
| `data/listings.json` | All discovered listings (shown in dashboard) |
| `data/sniper.log` | Full log |
