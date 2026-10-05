"""Draggable stop / target: validation, proposal edits, open-position modify queue, and the chart itself in a browser."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from src.config.settings import Settings
from src.data_ingestion.synthetic import make_bars
from src.db.schema import PendingOrder, Trade, get_engine, init_db, session_scope
from src.decision_engine.engine import Decision
from src.execution import control
from src.execution.sim_broker import SimBroker

S = Settings(max_stop_pct=0.05)


@pytest.fixture()
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path/'d.db'}")
    init_db(e)
    return e


# ------------------------------------------------------------------ validation
def test_validate_levels_sides_distance_and_widening():
    ok, _ = control.validate_levels("long", 100.0, 98.0, 104.0, 98.0, S)
    assert ok
    assert not control.validate_levels("long", 100.0, 101.0, None, 98.0, S)[0]            # stop above price
    assert not control.validate_levels("long", 100.0, 98.0, 99.0, 98.0, S)[0]             # target below price
    assert not control.validate_levels("short", 100.0, 99.0, None, 102.0, S)[0]           # short stop below price
    assert control.validate_levels("short", 100.0, 102.0, 96.0, 102.0, S)[0]
    assert not control.validate_levels("long", 100.0, 93.0, None, 98.0, S)[0]             # > 5% away
    assert not control.validate_levels("long", 100.0, 99.99, None, 98.0, S)[0]            # basically at the price
    assert not control.validate_levels("long", 100.0, 95.0, None, 98.0, S)[0]             # 2.5x the original 2.0 distance
    assert control.validate_levels("long", 100.0, 96.5, None, 98.0, S)[0]                 # 1.75x is fine
    assert control.validate_levels("long", 100.0, 99.5, None, 98.0, S)[0]                 # tightening is always fine


# -------------------------------------------------------------- proposals
def _pending(engine, direction="long", tp=104.0):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    d = Decision(symbol="AAA", trade=True, direction=direction, qty=10, entry=100.0,
                 stop_loss=98.0 if direction == "long" else 102.0, take_profit=tp, signal_time=now, reasons=[])
    return control.create_pending(engine, d, None, now=now)


def test_dragging_levels_on_a_proposal_changes_what_approve_will_send(engine):
    pid = _pending(engine)
    ok, why = control.update_pending_levels(engine, pid, 98.5, 105.5, S)
    assert ok, why
    p = control.list_pending(engine, "pending")[0]
    assert (p.stop_loss, p.take_profit) == (98.5, 105.5)
    d = control.decision_from_pending(p)
    assert (d.stop_loss, d.take_profit) == (98.5, 105.5)
    ok, why = control.update_pending_levels(engine, pid, 101.0, 105.5, S)
    assert not ok and "below" in why
    assert control.list_pending(engine, "pending")[0].stop_loss == 98.5  # the bad drag changed nothing


def test_a_stop_only_proposal_ignores_a_target(engine):
    pid = _pending(engine, tp=None)
    ok, _ = control.update_pending_levels(engine, pid, 98.5, 110.0, S)
    assert ok and control.list_pending(engine, "pending")[0].take_profit is None


def test_expired_or_decided_proposals_cannot_be_edited(engine):
    pid = _pending(engine)
    control.decide(engine, pid, False)
    assert not control.update_pending_levels(engine, pid, 98.5, 105.0, S)[0]


# ------------------------------------------------------- open positions
def _open_position(engine, broker, tp=104.0):
    broker.set_price("AAA", 100.0)
    res = broker.place_order("AAA", "buy", 10, 98.0, tp)
    with session_scope(engine) as s:
        s.add(Trade(symbol="AAA", direction="long", qty=10, entry_price=100.0, stop_loss=98.0, take_profit=tp, status="filled",
                    broker_order_id=res.id, entry_time=datetime.now(timezone.utc).replace(tzinfo=None)))
    return res


def test_modify_request_is_validated_queued_and_applied_by_the_scheduler(engine, tmp_path):
    from src.scheduler.run_loop import TradingCycle

    broker = SimBroker()
    _open_position(engine, broker)
    msgs = []
    cyc = TradingCycle(engine=engine, broker=broker, settings=S, fetch=lambda *a, **k: pd.DataFrame(),
                       notify=lambda m, level="info", engine=None, post=True: msgs.append((level, m)),
                       sentiment_fn=lambda s: dict(score=0.0, n=0), state_path=tmp_path / "st.json")
    rid, why = control.request_modify(engine, "AAA", 98.6, 105.0, settings=S)
    assert rid, why
    assert control.chart_levels(engine, "AAA")["positions"][0]["updating"] is True
    res = cyc.process_modifies()
    assert res and res[0]["applied"], res
    br = broker.brackets[0]
    assert (br.stop, br.target) == (98.6, 105.0)
    with session_scope(engine) as s:
        t = s.execute(select(Trade)).scalars().first()
    assert (t.stop_loss, t.take_profit) == (98.6, 105.0)
    assert any("MODIFY AAA" in m for _l, m in msgs)
    assert control.list_modify_requests(engine, "done")


def test_modify_is_refused_when_the_stop_is_beyond_the_market_or_there_is_no_position(engine, tmp_path):
    from src.scheduler.run_loop import TradingCycle

    broker = SimBroker()
    _open_position(engine, broker)
    cyc = TradingCycle(engine=engine, broker=broker, settings=S, fetch=lambda *a, **k: pd.DataFrame(),
                       notify=lambda *a, **k: None, sentiment_fn=lambda s: dict(score=0.0, n=0), state_path=tmp_path / "st.json")
    assert control.request_modify(engine, "AAA", 101.0, None, settings=S)[0] is None        # refused up front
    rid, _ = control.request_modify(engine, "AAA", 99.0, None, settings=S)
    broker.set_price("AAA", 98.5)                                                            # price falls through the new stop before it is applied
    res = cyc.process_modifies()
    assert res and not res[0]["applied"]
    assert broker.brackets[0].stop == 98.0                                                   # untouched
    broker.close_position("AAA")
    control.request_modify(engine, "AAA", 97.9, None, settings=S)
    res = cyc.process_modifies()
    assert res and not res[0]["applied"] and "no open position" in res[0]["why"]


def test_a_stop_only_position_shows_an_estimated_one_to_two_target(engine):
    broker = SimBroker()
    _open_position(engine, broker, tp=None)  # entry 100, stop 98 -> 1:2 target would be 104
    pos = control.chart_levels(engine, "AAA")["positions"][0]
    assert pos["target"] is None and pos["target_est"] == pytest.approx(104.0, abs=0.01)
    with session_scope(engine) as s:
        s.execute(select(Trade)).scalars().first().take_profit = 104.0
    assert control.chart_levels(engine, "AAA")["positions"][0]["target_est"] is None  # a real order replaces the estimate


def test_default_settings_put_a_one_to_two_take_profit_on_every_new_trade():
    from src.config.settings import Settings as Cfg

    assert Cfg().exit_mode == "hybrid" and Cfg().target_rr == 2.0


def test_chart_levels_lists_proposals_and_positions_for_the_symbol(engine):
    _pending(engine)
    _open_position(engine, SimBroker())
    lv = control.chart_levels(engine, "AAA")
    assert [p["stop"] for p in lv["pending"]] == [98.0] and lv["positions"][0]["target"] == 104.0
    assert control.chart_levels(engine, "ZZZ") == dict(pending=[], positions=[])


# -------------------------------------------------------- the chart in a browser
sync_api = pytest.importorskip("playwright.sync_api")


def _chart_page(tmp_path_factory, live):
    from src.dashboard import data as D
    from src.dashboard.chart_component import chart_html
    from src.smc_logic import compute_context

    bars = make_bars(n_days=40, seed=5)
    ctx = compute_context(bars)
    last = float(bars["close"].iat[-1])
    p = D.build_chart_payload(bars, ctx, pd.DataFrame(columns=["symbol"]), "AAA")
    p["live"] = live(last, int(pd.Timestamp(bars["timestamp"].iat[-20]).replace(tzinfo=timezone.utc).timestamp()))
    f = tmp_path_factory.mktemp("lv") / "chart.html"
    f.write_text(chart_html(p))
    return f, last


@pytest.fixture()
def browser_page():
    with sync_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available: {exc}")
        pg = br.new_page(viewport={"width": 1400, "height": 900})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        yield pg
        br.close()


def _drag(pg, price_from, to_dy, xfrac=0.45):
    bb = pg.query_selector("#chart").bounding_box()
    y0 = bb["y"] + pg.evaluate(f"window.__alphawave.y({price_from})")
    x = bb["x"] + bb["width"] * xfrac
    pg.mouse.move(x, y0)
    pg.mouse.down()
    pg.mouse.move(x, y0 + to_dy / 2, steps=4)
    pg.mouse.move(x, y0 + to_dy, steps=4)
    pg.mouse.up()


def test_dragging_a_proposal_line_posts_the_confirmed_levels(tmp_path_factory, browser_page):
    pg = browser_page
    f, last = _chart_page(tmp_path_factory, lambda px, t0: dict(
        pending=[dict(id=7, direction="long", qty=10, entry=px, stop=round(px * 0.99, 2), target=round(px * 1.02, 2))], positions=[]))
    pg.goto(f"file://{f}")
    pg.evaluate("window.addEventListener('message', e => { (window.__msgs = window.__msgs || []).push(e.data); })")
    pg.wait_for_timeout(1200)
    b = pg.evaluate("window.__alphawave.boxes[0]")
    assert "Confirm trade" in pg.inner_text("#acts")
    _drag(pg, b["edit"]["target"], -30)  # drag the take-profit up
    nb = pg.evaluate("window.__alphawave.boxes[0]")
    assert nb["edit"]["target"] > b["edit"]["target"] and nb["edit"]["stop"] == b["edit"]["stop"] and nb["dirty"]
    assert "Reset" in pg.inner_text("#acts")
    pg.click("button[data-a=confirm]")
    pg.wait_for_timeout(200)
    msgs = pg.evaluate("window.__msgs.map(m => m.alphawave).filter(Boolean)")
    assert msgs[-1]["type"] == "confirm_pending" and msgs[-1]["id"] == 7
    assert msgs[-1]["target"] == pytest.approx(nb["edit"]["target"]) and msgs[-1]["stop"] == pytest.approx(b["edit"]["stop"])
    assert "Sending" in pg.inner_text("#acts")
    assert pg.errors == []


def test_stop_cannot_be_dragged_across_the_entry_and_reset_restores_it(tmp_path_factory, browser_page):
    pg = browser_page
    f, last = _chart_page(tmp_path_factory, lambda px, t0: dict(
        pending=[dict(id=1, direction="long", qty=5, entry=px, stop=round(px * 0.99, 2), target=round(px * 1.02, 2))], positions=[]))
    pg.goto(f"file://{f}")
    pg.wait_for_timeout(1200)
    b = pg.evaluate("window.__alphawave.boxes[0]")
    _drag(pg, b["edit"]["stop"], -400)  # far above the entry
    nb = pg.evaluate("window.__alphawave.boxes[0]")
    assert nb["edit"]["stop"] < nb["entry"], "a long's stop must stay below its entry"
    pg.click("button[data-a=reset]")
    assert pg.evaluate("window.__alphawave.boxes[0].edit.stop") == b["edit"]["stop"]
    assert pg.errors == []


def test_open_position_drag_needs_apply_and_posts_a_modify(tmp_path_factory, browser_page):
    pg = browser_page
    f, last = _chart_page(tmp_path_factory, lambda px, t0: dict(pending=[], positions=[
        dict(id=3, direction="long", qty=10, entry=px, stop=round(px * 0.99, 2), target=round(px * 1.02, 2), t0=t0, updating=False)]))
    pg.goto(f"file://{f}")
    pg.evaluate("window.addEventListener('message', e => { (window.__msgs = window.__msgs || []).push(e.data); })")
    pg.wait_for_timeout(1200)
    b = pg.evaluate("window.__alphawave.boxes[0]")
    assert "Apply" not in pg.inner_text("#acts")
    _drag(pg, b["edit"]["stop"], -15, xfrac=0.9)  # tighten the stop (the line only exists from the entry bar rightwards)
    nb = pg.evaluate("window.__alphawave.boxes[0]")
    assert nb["edit"]["stop"] > b["edit"]["stop"]
    assert not pg.evaluate("(window.__msgs || []).some(m => m.alphawave)"), "dragging alone must not send anything"
    pg.click("button[data-a=apply]")
    pg.wait_for_timeout(200)
    m = pg.evaluate("window.__msgs.map(m => m.alphawave).filter(Boolean)")[-1]
    assert m["type"] == "modify_position" and m["symbol"] == "AAA" and m["stop"] == pytest.approx(nb["edit"]["stop"])
    assert pg.errors == []


# ------------------------------------------------- Alpaca order lookup (regression: stop leg hidden by nested query)
class _O:
    def __init__(self, id, symbol, side, otype, status, legs=None):
        from types import SimpleNamespace as N
        self.id, self.symbol, self.qty, self.filled_qty, self.filled_avg_price = id, symbol, 10, 0, None
        self.side, self.status, self.order_type, self.legs = N(value=side), N(value=status), N(value=otype), legs or []


class _FakeAlpaca:
    """Mimics Alpaca for a filled bracket: the 'open' filter lists only the take-profit leg (the stop leg is 'held' and
    hidden from it); the held stop leg is visible only nested under its filled parent in an ALL-status query."""
    def __init__(self):
        self.replaced = []

    def get_orders(self, req):
        status = getattr(req.status, "value", str(req.status)).lower()
        if "open" in status:
            return [_O("tp1", "ASTS", "sell", "limit", "new")] if not req.nested else []
        parent = _O("p1", "ASTS", "buy", "market", "filled",
                    legs=[_O("tp1", "ASTS", "sell", "limit", "new"), _O("stop1", "ASTS", "sell", "stop", "held")])
        return [parent] if req.nested else []

    def replace_order_by_id(self, oid, req):
        self.replaced.append((oid, req))


def test_modify_finds_the_held_stop_leg_that_the_open_filter_never_lists():
    from src.execution.alpaca_execution import AlpacaBroker
    fake = _FakeAlpaca()
    b = AlpacaBroker(S, client=fake)
    b.get_positions = lambda: [type("P", (), dict(symbol="ASTS", qty=86))()]   # a long position: exits are sells
    ok, msg = b.modify_exit_levels("ASTS", 56.9, 61.0)
    assert ok, msg
    assert sorted(o for o, _ in fake.replaced) == ["stop1", "tp1"]
    assert b.protected_symbols() == {"ASTS"}


def test_failed_modify_is_surfaced_on_the_dashboard(engine):
    from src.dashboard import alerts_ui
    from src.db.schema import ModifyRequest
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rid, _ = None, None
    with session_scope(engine) as sx:
        sx.add(ModifyRequest(created_at=now, symbol="ASTS", stop=60.0, target=None, status="failed", note="broker refused"))
    html = alerts_ui.modify_failure_html(control.list_modify_requests(engine, "failed"), now)
    assert "ASTS" in html and "NOT applied" in html and "broker refused" in html
    assert alerts_ui.modify_failure_html([], now) == ""
