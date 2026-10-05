"""Approve / Reject from Discord: authorization and decision logic (no network)."""
from datetime import datetime, timedelta

import pytest

from src.db.schema import PendingOrder, get_engine, init_db, session_scope
from src.discord_approvals import DiscordApprovals, custom_id, handle_click
from src.execution import control

ME, OTHER = 111, 222


@pytest.fixture
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(e)
    return e


def _pending(engine, minutes=10):
    now = datetime.utcnow()
    with session_scope(engine) as s:
        row = PendingOrder(created_at=now, expires_at=now + timedelta(minutes=minutes), symbol="SPY", direction="long",
                           qty=10, entry=500.0, stop_loss=495.0, take_profit=510.0, signal_time=now, status="pending")
        s.add(row)
        s.flush()
        return row.id


def _status(engine, pid):
    with session_scope(engine) as s:
        return s.get(PendingOrder, pid).status


def test_approve_by_allowed_user(engine):
    pid = _pending(engine)
    ok, msg = handle_click(engine, custom_id("approve", pid), ME, [ME], "rakesh")
    assert ok and "Approved" in msg and _status(engine, pid) == "approved"


def test_reject(engine):
    pid = _pending(engine)
    ok, msg = handle_click(engine, custom_id("reject", pid), ME, [ME])
    assert ok and "Rejected" in msg and _status(engine, pid) == "rejected"


def test_stranger_cannot_approve(engine):
    pid = _pending(engine)
    ok, msg = handle_click(engine, custom_id("approve", pid), OTHER, [ME])
    assert not ok and "not allowed" in msg and _status(engine, pid) == "pending"


def test_expired_or_already_decided(engine):
    pid = _pending(engine, minutes=-1)
    ok, msg = handle_click(engine, custom_id("approve", pid), ME, [ME])
    assert not ok and "Too late" in msg and _status(engine, pid) != "approved"
    pid2 = _pending(engine)
    control.decide(engine, pid2, True)
    ok, msg = handle_click(engine, custom_id("reject", pid2), ME, [ME])
    assert not ok and _status(engine, pid2) == "approved"


@pytest.mark.parametrize("cid", ["", "aw:approve", "aw:approve:x", "zz:approve:1", "aw:close:1"])
def test_bad_ids_are_ignored(engine, cid):
    assert handle_click(engine, cid, ME, [ME]) == (False, "Unknown button.")


def test_only_enabled_when_fully_configured():
    class S:
        discord_bot_token, discord_channel_id, discord_approver_ids = "t", 5, (ME,)

    assert DiscordApprovals.configured(S)
    S.discord_approver_ids = ()
    assert not DiscordApprovals.configured(S)  # no allowed users -> never enabled
