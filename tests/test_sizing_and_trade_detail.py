"""Editable position sizing, editable share count on a proposal, and the trade-details data behind the popup."""
from datetime import datetime, timedelta

import pytest

from src.config.settings import Settings
from src.dashboard import metrics as m
from src.db.schema import PendingOrder, SystemEvent, Trade, get_engine, init_db, session_scope
from src.execution import control

S = Settings()


@pytest.fixture()
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path/'d.db'}")
    init_db(e)
    return e


def test_sizing_defaults_edit_validate_and_reset(engine):
    assert control.get_risk(engine, S) == {"max_position_pct": S.max_position_pct, "risk_per_trade_pct": S.risk_per_trade_pct}
    ok, _ = control.set_risk(engine, 0.10, 0.01)
    assert ok
    eff = control.effective_settings(engine, S)
    assert (eff.max_position_pct, eff.risk_per_trade_pct) == (0.10, 0.01)
    assert S.max_position_pct == 0.05                                     # the .env settings object is untouched
    assert not control.set_risk(engine, 0.90, 0.01)[0]                    # 90% of equity in one position: refused
    assert not control.set_risk(engine, 0.05, 0.20)[0]                    # 20% risk per trade: refused
    assert control.effective_settings(engine, S).max_position_pct == 0.10  # a refused edit changed nothing
    control.reset_risk(engine)
    assert control.effective_settings(engine, S).max_position_pct == S.max_position_pct


def test_bigger_cap_gives_bigger_size_in_the_engine_formula(engine):
    import math
    equity, entry, risk = 100_000.0, 57.53, 1.01
    size = lambda s: min(math.floor(equity * s.risk_per_trade_pct / risk), math.floor(equity * s.max_position_pct / entry))
    before = size(control.effective_settings(engine, S))
    control.set_risk(engine, 0.10, S.risk_per_trade_pct)
    assert size(control.effective_settings(engine, S)) > before


def _pending(engine, qty=10):
    now = datetime.utcnow()
    with session_scope(engine) as sx:
        row = PendingOrder(symbol="AAA", direction="long", qty=qty, entry=100.0, stop_loss=98.0, take_profit=104.0,
                           expires_at=now + timedelta(minutes=10), status="pending", reasons_json="[]")
        sx.add(row)
        sx.flush()
        return row.id


def test_share_count_of_a_proposal_can_be_edited_within_limits(engine):
    pid = _pending(engine)
    assert control.update_pending_qty(engine, pid, 25)[0]
    assert control.list_pending(engine, "pending")[0].qty == 25
    assert not control.update_pending_qty(engine, pid, 0)[0]
    assert not control.update_pending_qty(engine, pid, 25 * 3 + 1)[0]
    assert control.list_pending(engine, "pending")[0].qty == 25


def test_trade_detail_collects_numbers_reasons_and_events(engine):
    n = datetime.utcnow()
    with session_scope(engine) as sx:
        t = Trade(symbol="SPY", direction="short", entry_time=n - timedelta(hours=2), exit_time=n - timedelta(hours=1),
                  entry_price=100.0, exit_price=96.0, qty=10, pnl=40.0, stop_loss=102.0, take_profit=96.0, status="closed")
        sx.add(t)
        sx.flush()
        tid = t.id
        sx.add(PendingOrder(symbol="SPY", direction="short", qty=10, expires_at=n, status="executed", trade_id=tid,
                            reasons_json='["three confirmations agree"]'))
        sx.add(SystemEvent(timestamp=n - timedelta(hours=2), kind="trade", message="SHORT SPY x10"))
        sx.add(SystemEvent(timestamp=n - timedelta(hours=2), kind="trade", message="LONG QQQ x1"))
    d = m.trade_detail(engine, tid)
    assert d["derived"]["r_multiple"] == pytest.approx(2.0)
    assert d["derived"]["risk_dollars"] == pytest.approx(20.0)
    assert d["derived"]["target_rr"] == pytest.approx(2.0)
    assert d["proposal"]["reasons"] == ["three confirmations agree"]
    assert [e["message"] for e in d["events"]] == ["SHORT SPY x10"]       # only this ticker's events
    assert m.trade_detail(engine, 9999) is None
