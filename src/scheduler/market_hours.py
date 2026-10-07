"""Trading-window logic: NYSE calendar + America/Chicago session (08:30-15:00 CT)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

from src.config import Settings, get_settings


@lru_cache(maxsize=1)
def _nyse():
    import pandas_market_calendars as mcal

    return mcal.get_calendar("NYSE")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _tz(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def to_local(now_utc_naive: datetime, settings: Settings | None = None) -> datetime:
    s = settings or get_settings()
    return now_utc_naive.replace(tzinfo=timezone.utc).astimezone(_tz(s))


def utc_now() -> datetime:
    """Naive-UTC 'now' (the convention used for every timestamp in this project)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_bounds(day: date, settings: Settings | None = None) -> tuple[datetime, datetime] | None:
    """(open, close) in local tz for `day`, or None if NYSE is closed.

    The configured window (default 08:30-15:00 CT) is intersected with the exchange hours,
    so early-close days (e.g. day after Thanksgiving) shorten the session automatically.
    """
    s = settings or get_settings()
    tz = _tz(s)
    sched = _nyse().schedule(start_date=day, end_date=day)
    if sched.empty:
        return None
    ex_open = sched["market_open"].iloc[0].tz_convert(tz).to_pydatetime()
    ex_close = sched["market_close"].iloc[0].tz_convert(tz).to_pydatetime()
    cfg_open = datetime.combine(day, _parse_hhmm(s.session_start), tzinfo=tz)
    cfg_close = datetime.combine(day, _parse_hhmm(s.session_end), tzinfo=tz)
    return max(ex_open, cfg_open), min(ex_close, cfg_close)


def is_trading_window_now(now: datetime | None = None, settings: Settings | None = None) -> bool:
    """True on NYSE trading days between session start (inclusive) and end (exclusive).

    `now` is naive UTC (default: the real clock).
    """
    s = settings or get_settings()
    local = to_local(now or utc_now(), s)
    b = session_bounds(local.date(), s)
    return bool(b and b[0] <= local < b[1])


def entry_cutoff(day: date, settings: Settings | None = None) -> datetime | None:
    s = settings or get_settings()
    b = session_bounds(day, s)
    return None if b is None else b[1] - timedelta(minutes=s.no_new_entries_minutes_before_close)


def flatten_time(day: date, settings: Settings | None = None) -> datetime | None:
    s = settings or get_settings()
    b = session_bounds(day, s)
    return None if b is None else b[1] - timedelta(minutes=s.flatten_minutes_before_close)


def late_entry_warning(now: datetime | None = None, settings: Settings | None = None) -> str:
    """Reminder for entries taken close to the end-of-day flatten ('' when not late, or flatten is off).

    `now` is naive UTC (default: the real clock). Positions are day trades: they are closed at the flatten time,
    not carried to the next day, so a late entry has little time to reach its target.
    """
    s = settings or get_settings()
    if not s.flatten_at_close:
        return ""
    local = to_local(now or utc_now(), s)
    ft = flatten_time(local.date(), s)
    if ft is None or local >= ft:
        return ""
    left = int((ft - local).total_seconds() // 60)
    if left > s.late_entry_warn_minutes:
        return ""
    h, m = divmod(left, 60)
    in_ = f"{h}h {m:02d}m" if h else f"{m} min"
    return (f"LATE ENTRY: all positions are closed at {ft.strftime('%H:%M')} {ft.tzname()} (in {in_}), "
            f"not held overnight - little time left to reach the target")


def can_open_new_positions(now: datetime | None = None, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    local = to_local(now or utc_now(), s)
    if not is_trading_window_now(now, s):
        return False
    cut = entry_cutoff(local.date(), s)
    return cut is not None and local < cut


def next_session_open(now: datetime | None = None, settings: Settings | None = None) -> datetime:
    """Local datetime of the next session open strictly after `now`."""
    s = settings or get_settings()
    local = to_local(now or utc_now(), s)
    d = local.date()
    for _ in range(10):
        b = session_bounds(d, s)
        if b and b[0] > local:
            return b[0]
        d += timedelta(days=1)
    raise RuntimeError("no trading session found in the next 10 days")


def bar_is_closed(bar_start_utc: datetime, timeframe_minutes: int, now_utc: datetime) -> bool:
    return bar_start_utc + timedelta(minutes=timeframe_minutes) <= now_utc
