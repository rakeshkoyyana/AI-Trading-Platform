"""Shadow analyst council: votes are correct, advisory only, logged beside decisions, and gradable."""
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from src.data_ingestion.synthetic import make_bars
from src.db.schema import CouncilVote, Signal, get_engine, init_db, session_scope
from src.decision_engine import council as C

LONG_GOOD = dict(rsi=55, vol_ratio=2.5, swing_trend=1, internal_trend=1, pd_position=0.2, in_bull_ob=True, in_bear_ob=False,
                 in_bull_fvg=False, in_bear_fvg=False)


@pytest.fixture()
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path/'c.db'}")
    init_db(e)
    return e


def test_votes_are_relative_to_the_signal_direction():
    v = C.feature_votes(LONG_GOOD, "long")
    assert all(x == 1 for x, _ in v.values()) and set(v) == {"momentum", "volume", "structure", "zone", "blocks"}
    # the same market against a short flips structure/zone/blocks
    s = C.feature_votes(LONG_GOOD, "short")
    assert s["structure"][0] == -1 and s["zone"][0] == -1 and s["blocks"][0] == -1


def test_momentum_room_before_the_rsi_exit_limits():
    assert C.feature_votes(dict(rsi=66), "long")["momentum"][0] == -1  # 4 pts from RSI 70: the Pine exit is near
    assert C.feature_votes(dict(rsi=52), "long")["momentum"][0] == 1
    assert C.feature_votes(dict(rsi=34), "short")["momentum"][0] == -1  # 4 pts above RSI 30
    assert C.feature_votes(dict(rsi=48), "short")["momentum"][0] == 1


def test_tally_thresholds_are_fixed():
    assert C.tally({"a": (1, ""), "b": (1, ""), "c": (0, "")})["verdict"] == "agree"
    assert C.tally({"a": (0, ""), "b": (0, "")})["verdict"] == "mixed"
    assert C.tally({"a": (-1, ""), "b": (0, ""), "c": (0, "")})["verdict"] == "disagree"  # -0.33 <= -0.20
    assert C.tally({})["verdict"] == "mixed"


def test_sentiment_and_htf_votes():
    assert C.sentiment_vote(dict(score=0.5, n=3), "long")[0] == 1
    assert C.sentiment_vote(dict(score=0.5, n=3), "short")[0] == -1
    assert C.sentiment_vote(dict(score=0.05, n=3), "long")[0] == 0
    assert C.sentiment_vote(dict(score=0.9, n=0), "long") is None
    bars = make_bars(n_days=60, seed=3)
    h = C.htf_vote(bars, "long")
    assert h is not None and h[0] in (-1, 0, 1) and "1H" in h[1]


def test_record_and_grade(engine):
    t0 = datetime(2026, 9, 1, 15, 0)
    with session_scope(engine) as s:
        for i, (won, feats) in enumerate([(1, LONG_GOOD), (1, LONG_GOOD), (0, dict(rsi=68, swing_trend=-1, internal_trend=-1,
                                                                                    pd_position=0.9)),
                                          (0, dict(rsi=68, swing_trend=-1, internal_trend=-1, pd_position=0.9))]):
            s.add(Signal(symbol="AAA", timeframe="15Min", timestamp=t0 + timedelta(hours=i), direction="long",
                         confirmation_details_json=feats, label_win=won))
    hist = C.historical_votes_frame(engine)
    sm = C.summarize_votes(hist)
    assert sm["n"] == 4 and sm["base_win"] == 0.5
    v = sm["verdicts"].set_index("verdict")
    assert v.loc["agree", "win_rate"] == 1.0 and v.loc["disagree", "win_rate"] == 0.0
    assert not sm["analysts"].empty

    # live logging joins to the signal's label by time
    res = C.council_for(LONG_GOOD, "long")
    C.record_vote(engine, "AAA", "15Min", t0, "long", res, "pending")
    C.record_vote(engine, "AAA", "15Min", t0, "long", res, "traded")  # idempotent update, not a duplicate
    with session_scope(engine) as s:
        rows = list(s.execute(select(CouncilVote)).scalars())
    assert len(rows) == 1 and rows[0].action == "traded" and json.loads(rows[0].votes_json)["volume"]["v"] == 1
    live = C.logged_votes_frame(engine)
    assert live["label"].iat[0] == 1 and C.get_vote(engine, "AAA", t0).verdict == "agree"
    assert C.summarize_votes(pd.DataFrame())["n"] == 0


def test_council_never_changes_the_decision_and_never_breaks_the_cycle(tmp_path):
    """Same run with the council on, then crippled: identical trades; a broken council only logs an event."""
    import dataclasses
    from datetime import timedelta as td

    from src.config.settings import Settings
    from src.execution.sim_broker import SimBroker
    from src.scheduler.run_loop import TradingCycle
    from src.db.schema import SystemEvent, Trade

    S = Settings(max_stop_pct=0.5, min_stop_atr=0.0, tickers=["AAA"], sip_delay_minutes=0, live_hybrid=False, default_trade_mode="auto")
    frame = make_bars(n_days=60, seed=3)

    def run(tag, break_council):
        eng = get_engine(f"sqlite:///{tmp_path}/{tag}.db")
        init_db(eng)
        clock = {"t": None}
        cyc = TradingCycle(engine=eng, broker=SimBroker(), settings=S, notify=lambda *a, **k: None,
                           fetch=lambda sym, tf, a, b: frame[frame["timestamp"] <= clock["t"]].reset_index(drop=True),
                           sentiment_fn=lambda s: dict(score=0.0, n=1), state_path=tmp_path / f"{tag}.json")
        if break_council:
            import src.scheduler.run_loop as rl

            orig = rl.council.council_for
            rl.council.council_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            for k in range(len(frame) - 300, len(frame), 4):
                ts = frame["timestamp"].iat[k]
                clock["t"] = ts
                cyc.broker.set_price("AAA", float(frame["close"].iat[k]))
                cyc.run_cycle(now=(ts + td(minutes=15, seconds=30)).to_pydatetime(), force=True)
        finally:
            if break_council:
                rl.council.council_for = orig
        with session_scope(eng) as s:
            trades = [(t.direction, round(t.entry_price, 4), t.qty) for t in s.execute(select(Trade)).scalars()]
            kinds = [e.message for e in s.execute(select(SystemEvent).where(SystemEvent.kind == "council")).scalars()]
            votes = len(list(s.execute(select(CouncilVote)).scalars()))
        return trades, kinds, votes

    a_trades, a_msgs, a_votes = run("on", False)
    b_trades, b_msgs, b_votes = run("broken", True)
    assert a_trades == b_trades and a_trades, "the council must not change what the engine does"
    assert a_votes >= 1 and any("council" in m for m in a_msgs)
    assert b_votes == 0 and any("failed" in m for m in b_msgs)
