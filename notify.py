#!/usr/bin/env python3
"""
notify.py — best-effort Telegram notifications for the Foosball Tracker.

One entry point, `notify_new_request(author, title, content)`, which DMs a
short plain-text message when someone posts a feature request.

The feature is OFF unless both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are
set in the environment (read at call time, so nothing is captured at import).
Plain text only — no `parse_mode` — so user-supplied titles/details can never
break the message with stray Markdown/HTML. Every failure is swallowed: a
notification must never affect the request that triggered it.

Python 3 standard library only. The bot token is never logged.
"""

import json
import os
import urllib.request

TELEGRAM_API = "https://api.telegram.org"
MAX_TEXT = 4000     # Telegram caps sendMessage near 4096; leave headroom
TIMEOUT = 5


def notify_new_request(author, title, content):
    """Best-effort Telegram DM about a new feature request. Never raises."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return   # feature off

    text = f"🏓 New request from {author}\n\n{title}"
    if content:
        text += f"\n\n{content}"
    _send(token, chat_id, text[:MAX_TEXT])


def _send(token, chat_id, text):
    """POST sendMessage. Any failure (network, HTTP, encoding) is swallowed."""
    try:
        data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API}/bot{token}/sendMessage", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "foosball-tracker")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except Exception:
        pass
