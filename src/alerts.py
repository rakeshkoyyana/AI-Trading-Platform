"""Discord webhook alerts + persisted system events (feeds the dashboard health panel)."""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from src.config import get_settings
from src.db.schema import SystemEvent, get_engine, session_scope

_EMOJI = {"info": "ℹ️", "session": "🔔", "trade": "📈", "warning": "⚠️", "error": "🚨", "halt": "⛔"}


def log_event(kind: str, message: str, engine=None) -> None:
    """Persist an event; never raises (logging must not break a trading cycle)."""
    try:
        with session_scope(engine or get_engine()) as s:
            s.add(
                SystemEvent(
                    timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    kind=kind,
                    message=message[:2000],
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[alerts] could not persist event: {exc}")


def notify(message: str, level: str = "info", engine=None, post=requests.post) -> bool:
    """Send to Discord (if configured) and always persist as a system event."""
    log_event("alert" if level in {"warning", "error", "halt"} else level, message, engine)
    url = get_settings().discord_webhook_url
    if not url:
        print(f"[alert:{level}] {message}")
        return False
    mode = get_settings().trading_mode.upper()
    text = f"{_EMOJI.get(level, '')} **[{mode}]** {message}"[:1900]
    try:
        r = post(url, json={"content": text}, timeout=10)
        return 200 <= r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        print(f"[alerts] discord post failed: {exc}")
        return False
