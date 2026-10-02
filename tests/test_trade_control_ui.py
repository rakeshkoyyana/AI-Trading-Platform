"""Dashboard Trade control: per-ticker Off/Ask/Auto and the Approve / Reject queue (Streamlit AppTest)."""
from datetime import datetime, timedelta
from pathlib import Path

from src import universe as U
from src.data_ingestion.synthetic import make_bars
from src.decision_engine.engine import Decision
from src.execution import control


def _app(tmp_path, monkeypatch, tickers="SPY,QQQ"):
    from streamlit.testing.v1 import AppTest

    import streamlit as st

    import src.config.settings as cfg
    from src.data_ingestion.backfill import save_bars
    from src.db.schema import get_engine, init_db

    db = f"sqlite:///{tmp_path/'ui.db'}"
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.setenv("TICKERS", tickers)
    monkeypatch.setattr(U, "ASSETS_PATH", tmp_path / "assets.json")
    monkeypatch.setattr(U, "WATCHLIST_PATH", tmp_path / "wl.json")
    monkeypatch.setattr(U, "load_assets", lambda *a, **k: [{"symbol": "SPY", "name": "SPDR"}, {"symbol": "QQQ", "name": "Invesco"}])
    import src.db.schema as schema

    monkeypatch.setattr(schema, "_engine", None)  # the app must open THIS database, not one cached by an earlier test
    cfg.get_settings.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()
    eng = get_engine(db)
    init_db(eng)
    for s in tickers.split(","):
        save_bars(eng, s, "15Min", make_bars(n_days=60, seed=4))
    path = str(Path(__file__).resolve().parents[1] / "src" / "dashboard" / "app.py")
    return AppTest.from_file(path, default_timeout=180), eng, cfg


def test_mode_selector_writes_the_mode_and_approval_buttons_decide(tmp_path, monkeypatch):
    at, eng, cfg = _app(tmp_path, monkeypatch)
    try:
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert at.segmented_control(key="mode_SPY").value == "Ask"  # DEFAULT_TRADE_MODE
        at.segmented_control(key="mode_SPY").set_value("Auto").run()
        at.segmented_control(key="mode_QQQ").set_value("Off").run()
        assert control.get_modes(eng, settings=cfg.get_settings()) == {"SPY": "Auto".lower(), "QQQ": "off"}

        now = datetime.utcnow()
        d = Decision(symbol="SPY", trade=True, direction="long", qty=3, entry=500.0, stop_loss=495.0,
                     probability=0.61, signal_time=now, reasons=["EMA bull", "stop 495"])
        a = control.create_pending(eng, d, None, ttl_minutes=10, now=now)
        d2 = Decision(symbol="QQQ", trade=True, direction="short", qty=2, entry=400.0, stop_loss=405.0,
                      signal_time=now - timedelta(minutes=15), reasons=[])
        b = control.create_pending(eng, d2, None, ttl_minutes=10, now=now)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        at.button(key=f"ap_{a}").click().run()
        at.button(key=f"rj_{b}").click().run()
        assert not at.exception, [e.value for e in at.exception]
        got = {p.id: p.status for p in control.list_pending(eng, None)}
        assert got == {a: "approved", b: "rejected"}
    finally:
        cfg.get_settings.cache_clear()


def test_banner_and_chime_fire_once_per_new_request_and_respect_the_toggle(tmp_path, monkeypatch):
    at, eng, cfg = _app(tmp_path, monkeypatch)
    try:
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert len(at.get("audio")) == 0
        now = datetime.utcnow()
        d = Decision(symbol="SPY", trade=True, direction="long", qty=3, entry=500.0, stop_loss=495.0,
                     signal_time=now, reasons=["x"])
        pid = control.create_pending(eng, d, None, ttl_minutes=10, now=now)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert len(at.get("audio")) == 1 and pid in at.session_state["alerted_ids"]  # chime on the NEW request
        at.run()
        assert len(at.get("audio")) == 0  # not repeated on every refresh
        # sound off: the banner still appears, the chime does not
        d2 = Decision(symbol="QQQ", trade=True, direction="short", qty=2, entry=400.0, stop_loss=405.0,
                      signal_time=now - timedelta(minutes=15), reasons=[])
        pid2 = control.create_pending(eng, d2, None, ttl_minutes=10, now=now)
        chime = [x for x in at.sidebar.toggle if "Chime" in x.label][0]
        chime.set_value(False).run()
        assert not at.exception, [e.value for e in at.exception]
        assert pid2 in at.session_state["alerted_ids"] and len(at.get("audio")) == 0
    finally:
        cfg.get_settings.cache_clear()


def test_chime_is_a_valid_short_wav_and_banner_text():
    import io
    import wave
    from types import SimpleNamespace

    from src.dashboard import alerts_ui

    with wave.open(io.BytesIO(alerts_ui.chime_wav())) as w:
        assert w.getnchannels() == 1 and 0.3 < w.getnframes() / w.getframerate() < 1.0
    assert alerts_ui.banner_html([], datetime.utcnow()) == ""
    p = SimpleNamespace(id=1, symbol="ASTS", direction="long", qty=40, entry=58.1, expires_at=datetime.utcnow() + timedelta(minutes=9))
    html = alerts_ui.banner_html([p], datetime.utcnow())
    assert "ASTS" in html and "LONG" in html and "waiting for your approval" in html and "banner alert" in html
    assert alerts_ui.new_alert_ids([p], {1}) == [] and alerts_ui.new_alert_ids([p], set()) == [1]


def test_close_button_needs_confirm_then_queues_a_close(tmp_path, monkeypatch):
    from src.db.schema import Trade, session_scope

    at, eng, cfg = _app(tmp_path, monkeypatch)
    try:
        with session_scope(eng) as s:
            s.add(Trade(symbol="SPY", direction="long", qty=10, entry_price=100.0, stop_loss=98.0, take_profit=104.0,
                        status="filled", entry_time=datetime.utcnow()))
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        at.button(key="cl_SPY").click().run()
        assert control.list_close_requests(eng, "pending") == []  # first click only asks to confirm
        at.button(key="cx_SPY").click().run()
        assert at.button(key="cl_SPY") is not None  # cancelled back to the plain button
        at.button(key="cl_SPY").click().run()
        at.button(key="cc_SPY").click().run()
        reqs = control.list_close_requests(eng, "pending")
        assert [r.symbol for r in reqs] == ["SPY"]
        assert "closing" in " ".join(c.value for c in at.caption).lower()
    finally:
        cfg.get_settings.cache_clear()
