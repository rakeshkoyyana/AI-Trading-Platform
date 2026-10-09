"""Entries taken late in the day get a reminder that positions are flattened before the close."""
import dataclasses
from datetime import datetime

from src.config.settings import Settings
from src.scheduler.market_hours import late_entry_warning

S = Settings()
WED = (2026, 10, 7)  # CDT: 13:00 CT = 18:00 UTC; flatten 14:55 CT = 19:55 UTC


def _utc(h, m=0):
    return datetime(*WED, h, m)


def test_no_warning_before_13_00_ct():
    assert late_entry_warning(_utc(17, 59), S) == ""


def test_warning_from_13_00_ct_names_the_flatten_time():
    msg = late_entry_warning(_utc(18, 0), S)
    assert "14:55" in msg and "CDT" in msg and "1h 55m" in msg and "overnight" in msg


def test_countdown_in_minutes_near_the_end():
    assert "in 20 min" in late_entry_warning(_utc(19, 35), S)


def test_no_warning_once_flatten_time_has_passed():
    assert late_entry_warning(_utc(19, 55), S) == ""


def test_no_warning_when_flatten_is_off_or_market_closed():
    assert late_entry_warning(_utc(18, 30), dataclasses.replace(S, flatten_at_close=False)) == ""
    assert late_entry_warning(datetime(2026, 10, 10, 18, 30), S) == ""  # Saturday


def test_threshold_is_configurable():
    assert late_entry_warning(_utc(18, 0), dataclasses.replace(S, late_entry_warn_minutes=60)) == ""
