"""Approval alerts inside the dashboard: a banner and a short chime (no external service, no cost)."""
from __future__ import annotations

import io
import math
import struct
import wave
from functools import lru_cache

from src.dashboard import theme as T


@lru_cache(maxsize=1)
def chime_wav() -> bytes:
    """A short two-note chime (880 Hz then 1320 Hz) as a 16-bit mono WAV, generated in code."""
    rate, notes = 22050, [(880.0, 0.16), (1320.0, 0.28)]
    frames = bytearray()
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (0.01 * rate)) * (1.0 - i / n) ** 1.5  # quick attack, smooth decay
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def banner_html(pending: list, now) -> str:
    """Attention banner for requests waiting on Approve / Reject (empty string when none)."""
    if not pending:
        return ""
    items = []
    for p in pending[:4]:
        left = max(int((p.expires_at - now).total_seconds()), 0)
        arrow = "▲" if p.direction == "long" else "▼"
        items.append(f"<b>{T.esc(p.symbol)}</b> {arrow} {p.direction.upper()} × {p.qty} @ ~{(p.entry or 0):.2f} "
                     f"<span class='mut'>({left // 60}:{left % 60:02d} left)</span>")
    more = f" +{len(pending) - 4} more" if len(pending) > 4 else ""
    n = len(pending)
    return (f'<div class="banner alert"><span class="bell">🔔</span> <b>{n} trade{"s" if n != 1 else ""} waiting for your approval</b>'
            f' &nbsp;·&nbsp; {" &nbsp;|&nbsp; ".join(items)}{more} &nbsp;·&nbsp; approve or reject below</div>')


def new_alert_ids(pending: list, seen: set) -> list[int]:
    """Pending ids not yet announced in this browser session."""
    return [p.id for p in pending if p.id not in seen]
