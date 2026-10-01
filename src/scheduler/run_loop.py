"""
Main trading loop.

    python -m src.scheduler.run_loop            # run unattended (APScheduler, America/Chicago)
    python -m src.scheduler.run_loop --once     # one cycle now (respects the trading window)
    python -m src.scheduler.run_loop --once --force --sim   # dry run, ignore window, simulated broker

Each cycle: reconcile with the broker -> top up bars -> compute SMC/triple-confirmation context
-> sentiment -> decision engine -> place bracket order -> log. Every symbol is isolated in its own
try/except so one bad ticker (or one bad cycle) never stops the day.
"""
from __future__ import annotations

import argparse
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src import alerts
from src.config import PROJECT_ROOT, Settings, get_settings
from src.data_ingestion import get_bars_with_fallback
from src.data_ingestion.backfill import backfill, load_bars
from src.data_ingestion.common import TIMEFRAME_MINUTES
from src.db.schema import ModelPrediction, Trade, get_engine, init_db, session_scope
from src.decision_engine.engine import AccountState, Decision, should_trade
from src.execution.base import Broker
from src.execution.trade_log import flatten_all, reconcile, record_order
from src.ml.predict import load_bundle
from src.scheduler.market_hours import (
    bar_is_closed,
    can_open_new_positions,
    flatten_time,
    is_trading_window_now,
    session_bounds,
    to_local,
    utc_now,
)
from src.sentiment.aggregate import get_rolling_sentiment, refresh_sentiment
from src.smc_logic.backfill_signals import backfill_signals, upsert_signal_event
from src.smc_logic.pipeline import compute_context

STATE_PATH = PROJECT_ROOT / "data" / "state.json"
MIN_BARS = 300  # ATR(200) + swing(50) warm-up


class TradingCycle:
    def __init__(
        self,
        engine=None,
        broker: Broker | None = None,
        settings: Settings | None = None,
        fetch=get_bars_with_fallback,
        notify=alerts.notify,
        bundle: dict | None = None,
        sentiment_fn=None,
        state_path: Path | None = None,
        history_days: int = 45,
    ):
        self.s = settings or get_settings()
        self.engine = engine or get_engine()
        init_db(self.engine)
        self.broker = broker
        self.fetch = fetch
        self.notify = lambda msg, level="info": notify(msg, level, engine=self.engine)
        self.bundle = bundle
        self.sentiment_fn = sentiment_fn or (lambda sym: get_rolling_sentiment(sym, engine=self.engine))
        self.state_path = state_path or STATE_PATH
        self.history_days = history_days
        self.tf_min = TIMEFRAME_MINUTES.get(self.s.timeframe, 15)
        self._flattened_day = None

    # ----------------------------------------------------------------- state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, **kw) -> None:
        st = {**self._load_state(), **kw}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st))

    def day_start_equity(self, now: datetime) -> float:
        today = str(to_local(now, self.s).date())
        st = self._load_state()
        if st.get("date") == today and st.get("day_start_equity"):
            return float(st["day_start_equity"])
        acct = self.broker.get_account()
        eq = acct.last_equity or acct.equity  # broker's prior-close equity is the cleanest baseline
        self._save_state(date=today, day_start_equity=eq)
        return float(eq)

    # --------------------------------------------------------------- session
    def start_session(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        local = to_local(now, self.s)
        if session_bounds(local.date(), self.s) is None:
            return
        self._flattened_day = None
        self._save_state(date=str(local.date()), day_start_equity=None)
        eq = self.day_start_equity(now)
        mode = "LIVE" if self.s.is_live else "PAPER"
        self.notify(f"Session started ({mode}). Day-start equity ${eq:,.2f}. Watching {', '.join(self.s.tickers)}.", "session")
        try:
            refresh_sentiment(self.s.tickers, engine=self.engine)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Sentiment refresh failed at session start: {exc}", "warning")

    def maybe_flatten(self, now: datetime | None = None) -> bool:
        """Flatten once per day at (close - flatten_minutes_before_close)."""
        now = now or utc_now()
        local = to_local(now, self.s)
        ft = flatten_time(local.date(), self.s)
        if not self.s.flatten_at_close or ft is None or local < ft or self._flattened_day == local.date():
            return False
        b = session_bounds(local.date(), self.s)
        if local >= b[1]:
            return False
        self._flattened_day = local.date()
        n = flatten_all(self.engine, self.broker, self.notify)
        alerts.log_event("session", f"flatten job ran; closed {n} trade(s)", self.engine)
        return True

    def end_session(self, now: datetime | None = None) -> dict:
        now = now or utc_now()
        local = to_local(now, self.s)
        if session_bounds(local.date(), self.s) is None:
            return {}
        issues = reconcile(self.engine, self.broker, self.notify)
        start_utc = datetime.combine(local.date(), datetime.min.time()) + timedelta(hours=5)
        with session_scope(self.engine) as sx:
            closed = list(sx.execute(select(Trade).where(Trade.status == "closed", Trade.exit_time >= start_utc)).scalars())
        pnl = sum(t.pnl or 0.0 for t in closed)
        wins = sum(1 for t in closed if (t.pnl or 0) > 0)
        acct = self.broker.get_account()
        summary = dict(trades=len(closed), wins=wins, pnl=pnl, equity=acct.equity, issues=len(issues))
        self.notify(
            f"Session ended. {len(closed)} closed trade(s), {wins} win(s), P&L ${pnl:,.2f}. "
            f"Equity ${acct.equity:,.2f}. Reconciliation issues: {len(issues)}.", "session",
        )
        try:  # post-close maintenance: label new signals as forward bars exist
            backfill(self.s.tickers, self.s.timeframe, months=1, incremental=True, engine=self.engine, fetch=self.fetch)
            backfill_signals(self.s.tickers, self.s.timeframe, engine=self.engine)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Post-close maintenance failed: {exc}", "warning")
        return summary

    # ----------------------------------------------------------------- cycle
    def run_cycle(self, now: datetime | None = None, force: bool = False) -> dict:
        now = now or utc_now()
        if not force and not is_trading_window_now(now, self.s):
            return dict(skipped="outside trading window")
        out = dict(time=str(now), symbols={}, trades=0, signals=0)
        try:
            issues = reconcile(self.engine, self.broker, self.notify)
            acct = self.broker.get_account()
            positions = {p.symbol: p.qty for p in self.broker.get_positions()}
            state = AccountState(acct.equity, acct.buying_power, self.day_start_equity(now), positions)
            bundle = self.bundle if self.bundle is not None else load_bundle()
            can_open = force or can_open_new_positions(now, self.s)
            for sym in self.s.tickers:
                try:
                    res = self._process_symbol(sym, now, state, bundle, can_open)
                except Exception as exc:  # noqa: BLE001
                    res = dict(error=str(exc))
                    self.notify(f"{sym}: cycle error: {exc}\n{traceback.format_exc()[-600:]}", "error")
                out["symbols"][sym] = res
                out["signals"] += int(bool(res.get("signal")))
                out["trades"] += int(bool(res.get("traded")))
            out["reconcile_issues"] = len(issues)
            alerts.log_event(
                "cycle",
                f"{len(self.s.tickers)} symbols, {out['signals']} signal(s), {out['trades']} trade(s), "
                f"equity ${acct.equity:,.2f}, model={'yes' if bundle else 'none'}",
                self.engine,
            )
        except Exception as exc:  # noqa: BLE001 - never let one cycle kill the process
            self.notify(f"Cycle failed: {exc}\n{traceback.format_exc()[-800:]}", "error")
            out["error"] = str(exc)
        return out

    def _closed_bars(self, sym: str, now: datetime) -> pd.DataFrame:
        backfill([sym], self.s.timeframe, months=1, incremental=True, engine=self.engine, fetch=self.fetch)
        bars = load_bars(self.engine, sym, self.s.timeframe, since=now - timedelta(days=self.history_days))
        while len(bars) and not bar_is_closed(bars["timestamp"].iat[-1].to_pydatetime(), self.tf_min, now):
            bars = bars.iloc[:-1]  # drop the still-forming bar
        return bars.reset_index(drop=True)

    def _process_symbol(self, sym: str, now: datetime, state: AccountState, bundle, can_open: bool) -> dict:
        bars = self._closed_bars(sym, now)
        if len(bars) < MIN_BARS:
            return dict(skipped=f"only {len(bars)} bars (< {MIN_BARS})")
        ctx = compute_context(bars)
        sig_id = upsert_signal_event(self.engine, sym, self.s.timeframe, ctx)
        sentiment = self.sentiment_fn(sym)
        d = should_trade(sym, ctx, state, self.s, bundle, sentiment, now, self.s.timeframe)
        if d.trade and not can_open:
            d.block("session_cutoff", "no new entries this close to the end of the session")

        res = dict(signal=ctx["signal_event"].iat[-1] != "none", traded=False, blocked_by=d.blocked_by)
        if res["signal"]:
            alerts.log_event("decision", d.explain(), self.engine)
            if d.probability is not None and sig_id:
                with session_scope(self.engine) as sx:
                    sx.add(ModelPrediction(signal_id=sig_id, probability=d.probability,
                                           model_version=(bundle or {}).get("version", "none")))
        if not d.trade:
            return res

        side = "buy" if d.direction == "long" else "sell"
        order = self.broker.place_order(sym, side, d.qty, d.stop_loss, d.take_profit)
        tid = record_order(self.engine, d, order, "live" if self.s.is_live else "paper", sig_id)
        if order.is_rejected or not order.id:
            self.notify(f"{sym}: order REJECTED ({order.message or order.status})", "warning")
            res["order"] = "rejected"
            return res
        state.open_positions[sym] = d.qty if d.direction == "long" else -d.qty
        res.update(traded=True, trade_id=tid, qty=d.qty)
        p = f"P(win) {d.probability:.2f}, " if d.probability is not None else ""
        self.notify(
            f"{d.direction.upper()} {sym} x{d.qty} @ ~{d.entry:.2f} | stop {d.stop_loss} ({d.stop_source}) "
            f"| target {d.take_profit} ({d.target_source}) | {p}R:R {d.reward_risk:.2f}",
            "trade",
        )
        return res


# ------------------------------------------------------------------ scheduler
def _cron_minutes(tf_min: int) -> str:
    if tf_min >= 60:
        return "0"
    return ",".join(str(m) for m in range(0, 60, tf_min))


def build_scheduler(cycle: TradingCycle):
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    s = cycle.s
    tz = s.timezone
    sched = BlockingScheduler(timezone=tz)
    dow = "mon-fri"
    sh, sm = (int(x) for x in s.session_start.split(":"))
    eh, em = (int(x) for x in s.session_end.split(":"))

    sched.add_job(cycle.start_session, CronTrigger(day_of_week=dow, hour=sh, minute=max(sm - 5, 0), timezone=tz),
                  id="session_start", misfire_grace_time=600)
    sched.add_job(cycle.run_cycle,
                  CronTrigger(day_of_week=dow, hour=f"{sh}-{eh - 1}", minute=_cron_minutes(cycle.tf_min),
                              second=s.bar_delay_seconds, timezone=tz),
                  id="cycle", max_instances=1, coalesce=True, misfire_grace_time=120)
    sched.add_job(cycle.maybe_flatten, CronTrigger(day_of_week=dow, hour=f"{sh}-{eh}", minute="*", timezone=tz),
                  id="flatten", max_instances=1, coalesce=True)
    sched.add_job(cycle.end_session, CronTrigger(day_of_week=dow, hour=eh, minute=em + 2, timezone=tz),
                  id="session_end", misfire_grace_time=1800)
    sched.add_job(lambda: refresh_sentiment(s.tickers, engine=cycle.engine),
                  CronTrigger(day_of_week=dow, hour=f"{sh}-{eh - 1}", minute=5, timezone=tz),
                  id="sentiment", max_instances=1, coalesce=True)

    def on_event(ev):
        kind = "error" if ev.code == EVENT_JOB_ERROR else "warning"
        detail = getattr(ev, "exception", None) or "missed run"
        cycle.notify(f"Scheduler job '{ev.job_id}' {'failed' if kind == 'error' else 'missed'}: {detail}", kind)

    sched.add_listener(on_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return sched


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--force", action="store_true", help="ignore the trading-window check (with --once)")
    p.add_argument("--sim", action="store_true", help="use the in-memory simulated broker")
    a = p.parse_args()

    if a.sim:
        import os

        os.environ["BROKER"] = "sim"
    from src.execution.alpaca_execution import get_broker

    s = get_settings()
    cycle = TradingCycle(broker=get_broker(s), settings=s)
    mode = "LIVE" if s.is_live else "PAPER"
    print(f"[run_loop] mode={mode} broker={cycle.broker.name} tickers={s.tickers} tf={s.timeframe}")
    if a.once:
        print(json.dumps(cycle.run_cycle(force=a.force), indent=2, default=str))
        return
    alerts.log_event("session", f"scheduler process started ({mode})", cycle.engine)
    cycle.notify(f"Scheduler started ({mode}, {cycle.broker.name}).", "info")
    sched = build_scheduler(cycle)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        cycle.notify("Scheduler stopped.", "info")


if __name__ == "__main__":
    main()
