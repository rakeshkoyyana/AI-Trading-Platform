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
import time
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
from src.data_ingestion.live_tail import with_live_tail
from src.db.schema import Bar, ModelPrediction, Trade, get_engine, init_db, session_scope
from src.discord_approvals import DiscordApprovals
from src.decision_engine import council
from src.decision_engine.engine import AccountState, Decision, pine_exit_reason, should_trade
from src.execution import control
from src.execution.base import Broker
from src.execution.trade_log import close_trade, flatten_all, reconcile, record_order
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
from src.smc_logic.config import SMCConfig
from src.smc_logic.pipeline import compute_context

def gating_bundle(bundle: dict | None, settings: Settings) -> tuple[dict | None, str]:
    """Only let a model gate trades if it beat the raw signal out of sample (or the user opts in)."""
    if not bundle:
        return None, "none"
    improves = bundle.get("improves", (bundle.get("metrics") or {}).get("improves"))
    if improves is False and not settings.use_unvalidated_model:
        return None, f"ignored ({bundle.get('version', '?')} did not beat raw signals OOS)"
    return bundle, bundle.get("version", "yes")


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
        fetch_live=None,
        price_fn=None,
    ):
        self.s = settings or get_settings()
        self.engine = engine or get_engine()
        init_db(self.engine)
        self.broker = broker
        self.fetch = fetch
        self.notify = lambda msg, level="info": notify(msg, level, engine=self.engine)
        self.approvals_bot = None  # set by main() when the Discord approval bot is configured
        self.bundle = bundle
        self.sentiment_fn = sentiment_fn or (lambda sym: get_rolling_sentiment(sym, engine=self.engine))
        self.state_path = state_path or STATE_PATH
        self.history_days = history_days
        self.tf_min = TIMEFRAME_MINUTES.get(self.s.timeframe, 15)
        self._flattened_day = None
        # live tail: newest bars from the real-time IEX feed (estimate), injected for tests
        self.fetch_live = fetch_live if fetch_live is not None else (with_live_tail if self.s.live_hybrid else None)
        self.price_fn = price_fn
        self._smc = SMCConfig()

    # --------------------------------------------------------------- universe
    def modes(self) -> dict[str, str]:
        return control.get_modes(self.engine, settings=self.s)

    def universe(self, held=()) -> list[str]:
        """Symbols to process: everything not 'off', plus anything we still hold (exits must run)."""
        active = [k for k, v in self.modes().items() if v != "off"]
        return list(dict.fromkeys([*active, *held]))

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
        modes = self.modes()
        desc = ", ".join(f"{k}:{v}" for k, v in modes.items() if v != "off") or "none (all Off)"
        self.notify(f"Session started ({mode}). Day-start equity ${eq:,.2f}. Trade modes: {desc}.", "session")
        try:
            refresh_sentiment(self.universe(), engine=self.engine)
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
            syms = self.universe()
            backfill(syms, self.s.timeframe, months=1, incremental=True, engine=self.engine, fetch=self.fetch)
            backfill_signals(syms, self.s.timeframe, engine=self.engine)
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
            bundle, model_note = gating_bundle(self.bundle if self.bundle is not None else load_bundle(), self.s)
            can_open = force or can_open_new_positions(now, self.s)
            modes = self.modes()
            self._modes_now = modes
            symbols = self.universe(held=[k for k, q in positions.items() if q])
            control.expire_stale(self.engine)
            for sym in symbols:
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
                f"{len(symbols)} symbols, {out['signals']} signal(s), {out['trades']} trade(s), "
                f"equity ${acct.equity:,.2f}, model={model_note}",
                self.engine,
            )
        except Exception as exc:  # noqa: BLE001 - never let one cycle kill the process
            self.notify(f"Cycle failed: {exc}\n{traceback.format_exc()[-800:]}", "error")
            out["error"] = str(exc)
        return out

    def _closed_bars(self, sym: str, now: datetime) -> pd.DataFrame:
        backfill([sym], self.s.timeframe, months=1, incremental=True, engine=self.engine, fetch=self.fetch)
        bars = load_bars(self.engine, sym, self.s.timeframe, since=now - timedelta(days=self.history_days))
        self._est = False
        if self.fetch_live is not None and len(bars):
            try:
                live, diag = self.fetch_live(bars, sym, self.s.timeframe, now=now)
                if "est" in live.columns:
                    bars = live
                if diag.get("n_tail"):
                    alerts.log_event("live_tail", f"{sym}: +{diag['n_tail']} est. bar(s), k={diag['k']:.1f}, "
                                     f"price MAPE {diag['price_mape']:.3%}, spike agree {diag['spike_agree']:.0%}", self.engine)
                elif diag.get("reason") and not str(diag["reason"]).startswith("no "):
                    alerts.log_event("live_tail", f"{sym}: no live tail ({diag['reason']})", self.engine)
            except Exception as exc:  # noqa: BLE001 - never block the cycle on the estimate
                alerts.log_event("live_tail", f"{sym}: live tail failed: {exc}", self.engine)
        horizon = now - timedelta(minutes=self.s.live_delay_minutes)  # data newer than this is not available/complete
        while len(bars) and not bar_is_closed(bars["timestamp"].iat[-1].to_pydatetime(), self.tf_min, horizon):
            bars = bars.iloc[:-1]  # drop the still-forming bar
        if "est" in bars.columns:
            self._est = bool(len(bars) and bars["est"].iat[-1])
            bars = bars.drop(columns="est")
        return bars.reset_index(drop=True)

    # ------------------------------------------------------------------ exits
    def _open_trade_id(self, sym: str) -> int | None:
        with session_scope(self.engine) as sx:
            t = sx.execute(select(Trade).where(Trade.symbol == sym, Trade.status.in_(["open", "filled"]))
                           .order_by(Trade.id.desc())).scalars().first()
            return t.id if t else None

    def _manage_exit(self, sym: str, ctx: pd.DataFrame, now: datetime, state: AccountState) -> str | None:
        """Pine exits: close a held position on RSI >= 70 / <= 30 or a trend flip. Runs for every mode."""
        qty = state.open_positions.get(sym) or 0
        if not qty or self.s.exit_mode not in ("pine", "hybrid"):
            return None
        row = ctx.iloc[-1]
        reason = pine_exit_reason(row, "long" if qty > 0 else "short", self._smc)
        if not reason:
            return None
        tid = self._open_trade_id(sym)
        res = self.broker.close_position(sym)
        if res is None:
            self.notify(f"{sym}: Pine exit ({reason}) but the close order failed - check the broker", "error")
            return None
        px = res.filled_avg_price or float(row["close"])
        if tid:
            close_trade(self.engine, tid, px, f"Pine exit: {reason}")
        state.open_positions.pop(sym, None)
        self.notify(f"CLOSE {sym} ({'long' if qty > 0 else 'short'}) ~{px:.2f} | {reason}", "trade")
        return reason

    # ---------------------------------------------------------------- orders
    def _execute(self, d: Decision, sig_id: int | None, state: AccountState) -> dict:
        side = "buy" if d.direction == "long" else "sell"
        order = self.broker.place_order(d.symbol, side, d.qty, d.stop_loss, d.take_profit)
        tid = record_order(self.engine, d, order, "live" if self.s.is_live else "paper", sig_id)
        if order.is_rejected or not order.id:
            self.notify(f"{d.symbol}: order REJECTED ({order.message or order.status})", "warning")
            return dict(order="rejected", trade_id=tid)
        state.open_positions[d.symbol] = d.qty if d.direction == "long" else -d.qty
        p = f"P(win) {d.probability:.2f}, " if d.probability is not None else ""
        tgt = f"target {d.take_profit} ({d.target_source})" if d.take_profit else "exit by Pine rules (RSI 70/30, trend flip)"
        self.notify(f"{d.direction.upper()} {d.symbol} x{d.qty} @ ~{d.entry:.2f} | stop {d.stop_loss} "
                    f"({d.stop_source or 'saved'}) | {tgt} | {p}", "trade")
        return dict(traded=True, trade_id=tid, qty=d.qty)

    def _process_symbol(self, sym: str, now: datetime, state: AccountState, bundle, can_open: bool) -> dict:
        bars = self._closed_bars(sym, now)
        if len(bars) < MIN_BARS:
            return dict(skipped=f"only {len(bars)} bars (< {MIN_BARS})")
        ctx = compute_context(bars)
        sig_id = upsert_signal_event(self.engine, sym, self.s.timeframe, ctx)
        mode = (getattr(self, "_modes_now", None) or self.modes()).get(sym, control.default_mode(sym, self.s))
        exited = self._manage_exit(sym, ctx, now, state)

        sentiment = self.sentiment_fn(sym)
        d = should_trade(sym, ctx, state, control.effective_settings(self.engine, self.s), bundle, sentiment, now, self.s.timeframe)
        if d.trade and not can_open:
            d.block("session_cutoff", "no new entries this close to the end of the session")
        if d.trade and mode == "off":
            d.block("mode_off", "trading is switched Off for this ticker on the dashboard")

        res = dict(signal=ctx["signal_event"].iat[-1] != "none", traded=False, blocked_by=d.blocked_by,
                   mode=mode, estimated=self._est)
        if exited:
            res["exit"] = exited
        if res["signal"]:
            alerts.log_event("decision", d.explain(), self.engine)
            if d.probability is not None and sig_id:
                with session_scope(self.engine) as sx:
                    sx.add(ModelPrediction(signal_id=sig_id, probability=d.probability,
                                           model_version=(bundle or {}).get("version", "none")))
        action = "traded" if d.trade and mode == "auto" else ("pending" if d.trade and mode == "ask" else f"blocked:{d.blocked_by}")
        if res["signal"]:
            self._shadow_council(sym, ctx, bars, d, sentiment, action)
        if not d.trade:
            return res

        if mode == "ask":
            pid = control.create_pending(self.engine, d, sig_id, settings=self.s)
            if pid:
                res["pending"] = pid
                tgt = f"stop {d.stop_loss}" + ("" if d.take_profit is None else f", target {d.take_profit}")
                text = (f"APPROVE? {d.direction.upper()} {sym} x{d.qty} @ ~{d.entry:.2f} | {tgt} | "
                        f"expires in {self.s.approval_ttl_minutes} min")
                if self.approvals_bot and self.approvals_bot.post_proposal(pid, f"📈 **[{self.s.trading_mode.upper()}]** {text}"):
                    alerts.log_event("trade", text, self.engine)  # buttons posted in Discord; dashboard works too
                else:
                    self.notify(text + " | open the dashboard to approve", "trade")
            return res
        out = self._execute(d, sig_id, state)
        res.update(out)
        return res

    def _shadow_council(self, sym, ctx, bars, d, sentiment, action) -> None:
        """Free rule-based analyst votes, logged beside the real decision. Advisory: never alters it, never raises."""
        try:
            from src.smc_logic.pipeline import FEATURE_COLS

            row = ctx.iloc[-1]
            feats = {c: row[c] for c in FEATURE_COLS}
            res = council.council_for(feats, row["signal_event"], bars=bars, sentiment=sentiment)
            st = pd.Timestamp(row["timestamp"]).to_pydatetime()
            council.record_vote(self.engine, sym, self.s.timeframe, st, row["signal_event"], res, action)
            alerts.log_event("council", f"{sym} {row['signal_event']}: council {res['verdict']} ({res['score']:+.2f}) "
                             f"vs engine {action}", self.engine)
        except Exception as exc:  # noqa: BLE001
            alerts.log_event("council", f"{sym}: shadow council failed: {exc}", self.engine)

    # ------------------------------------------------- fast reconcile + stop watch
    def quick_reconcile(self, now: datetime | None = None) -> list[str]:
        """Every ~30 s while a position is open: book stop / target fills as they happen and verify each position
        still has its protective stop resting at the broker (the stop itself lives at the broker and needs no scheduler)."""
        from sqlalchemy import func

        with session_scope(self.engine) as sx:
            n_open = sx.execute(select(func.count()).select_from(Trade).where(Trade.status.in_(["open", "filled"]))).scalar()
        warned = self.__dict__.setdefault("_unprotected", set())
        if not n_open:
            warned.clear()
            return []
        issues = reconcile(self.engine, self.broker, None)
        seen = self.__dict__.setdefault("_seen_issues", set())
        for msg in issues:
            if msg not in seen:
                seen.add(msg)
                self.notify("Reconciliation mismatch:\n" + msg, "warning")
        prot = self.broker.protected_symbols()
        if prot is not None:
            held = {p.symbol for p in self.broker.get_positions() if p.qty}
            missing = held - prot
            for sym in sorted(missing - warned):
                self.notify(f"{sym}: NO protective stop is resting at the broker - check Alpaca now", "error")
            warned.clear()
            warned.update(missing)
        return issues

    # ------------------------------------------------ chart-dragged stop / target
    def process_modifies(self, now: datetime | None = None) -> list[dict]:
        """Apply stop / target changes dragged on the chart to the open position's resting orders at the broker."""
        now = now or utc_now()
        results: list[dict] = []
        control.expire_modify_requests(self.engine, now)
        reqs = control.list_modify_requests(self.engine, "pending")
        if not reqs:
            return results
        positions = {p.symbol: p for p in self.broker.get_positions()}
        for r in sorted(reqs, key=lambda x: x.created_at):  # oldest first, so the newest drag wins
            pos = positions.get(r.symbol)
            if pos is None or not pos.qty:
                control.mark_modify(self.engine, r.id, "failed", "no open position at the broker")
                results.append(dict(id=r.id, symbol=r.symbol, applied=False, why="no open position at the broker"))
                continue
            with session_scope(self.engine) as sx:
                t = sx.execute(select(Trade).where(Trade.symbol == r.symbol, Trade.status.in_(["open", "filled"]))
                               .order_by(Trade.id.desc())).scalars().first()
                old_stop, old_tp, tid = (t.stop_loss, t.take_profit, t.id) if t else (None, None, None)
            ref = pos.market_price or pos.avg_entry_price
            ok, why = control.validate_levels(pos.direction, ref, r.stop, r.target if old_tp else None, old_stop, self.s)
            if not ok:
                control.mark_modify(self.engine, r.id, "failed", why)
                self.notify(f"{r.symbol}: stop/target change NOT applied - {why}", "warning")
                results.append(dict(id=r.id, symbol=r.symbol, applied=False, why=why))
                continue
            ok, msg = self.broker.modify_exit_levels(r.symbol, r.stop, r.target)
            if not ok:
                control.mark_modify(self.engine, r.id, "failed", msg)
                self.notify(f"{r.symbol}: stop/target change FAILED - {msg}", "error")
                results.append(dict(id=r.id, symbol=r.symbol, applied=False, why=msg))
                continue
            with session_scope(self.engine) as sx:
                t = sx.get(Trade, tid) if tid else None
                if t is not None:
                    if r.stop is not None:
                        t.stop_loss = round(float(r.stop), 2)
                    if r.target is not None and t.take_profit is not None:
                        t.take_profit = round(float(r.target), 2)
            control.mark_modify(self.engine, r.id, "done", msg)
            self.notify(f"MODIFY {r.symbol}: {msg} (was stop {old_stop}, target {old_tp})", "trade")
            results.append(dict(id=r.id, symbol=r.symbol, applied=True))
        return results

    # ------------------------------------------------------- manual closes
    def _exit_price(self, res, sym: str) -> float | None:
        """Fill price of a just-sent market close. The broker often answers before the fill, so poll the order briefly,
        then fall back to the latest known price (never leave the trade open or book it at the entry price)."""
        px = res.filled_avg_price
        for _ in range(6):
            if px:
                return float(px)
            time.sleep(1.0)
            o = self.broker.get_order(res.id) if res.id else None
            px = o.filled_avg_price if o else None
        if px:
            return float(px)
        if self.price_fn:
            try:
                p = self.price_fn(sym)
                if p:
                    return float(p)
            except Exception:  # noqa: BLE001
                pass
        with session_scope(self.engine) as sx:
            bar = sx.execute(select(Bar).where(Bar.symbol == sym).order_by(Bar.timestamp.desc())).scalars().first()
            return float(bar.close) if bar else None

    def process_closes(self, now: datetime | None = None, force: bool = False) -> list[dict]:
        """Execute 'Close position' clicks from the dashboard (market close + cancel the protective orders)."""
        now = now or utc_now()
        results: list[dict] = []
        control.expire_close_requests(self.engine, now)
        reqs = control.list_close_requests(self.engine, "pending")
        if not reqs:
            return results
        positions = {p.symbol: p.qty for p in self.broker.get_positions()}
        for r in reqs:
            qty = positions.get(r.symbol) or 0
            if not qty:
                control.mark_close(self.engine, r.id, "failed", "no open position at the broker")
                results.append(dict(id=r.id, symbol=r.symbol, closed=False))
                continue
            if not force and not is_trading_window_now(now, self.s):
                control.mark_close(self.engine, r.id, "failed", "market closed - a close order would only queue until the open")
                self.notify(f"{r.symbol}: manual close NOT sent - market closed", "warning")
                results.append(dict(id=r.id, symbol=r.symbol, closed=False))
                continue
            tid = self._open_trade_id(r.symbol)
            res = self.broker.close_position(r.symbol)
            if res is None:
                control.mark_close(self.engine, r.id, "failed", "the broker did not accept the close order")
                self.notify(f"{r.symbol}: manual close FAILED - check the broker", "error")
                results.append(dict(id=r.id, symbol=r.symbol, closed=False))
                continue
            px = self._exit_price(res, r.symbol)
            if tid and px:
                close_trade(self.engine, tid, px, "Manual close from dashboard")
            control.mark_close(self.engine, r.id, "done", f"closed ~{px:.2f}" if px else "close order sent")
            self.notify(f"CLOSE {r.symbol} ({'long' if qty > 0 else 'short'}) ~{px or 0:.2f} | manual close from the dashboard", "trade")
            results.append(dict(id=r.id, symbol=r.symbol, closed=True))
        return results

    # -------------------------------------------------------------- approvals
    def process_approvals(self, now: datetime | None = None, force: bool = False) -> list[dict]:
        """Execute entries the user approved on the dashboard (re-validated at the moment of sending)."""
        now = now or utc_now()
        results: list[dict] = []
        control.expire_stale(self.engine)
        approved = control.list_pending(self.engine, "approved")
        if not approved:
            return results
        if not force and not is_trading_window_now(now, self.s):
            for p in approved:
                control.mark(self.engine, p.id, "failed", "approved outside the trading window")
            return results
        can_open = force or can_open_new_positions(now, self.s)
        acct = self.broker.get_account()
        positions = {p.symbol: p.qty for p in self.broker.get_positions()}
        state = AccountState(acct.equity, acct.buying_power, self.day_start_equity(now), positions)
        for p in approved:
            why = None
            if not can_open:
                why = "too close to the end of the session"
            elif positions.get(p.symbol):
                why = "already holding this ticker"
            elif len([q for q in positions.values() if q]) >= self.s.max_open_positions:
                why = "max open positions reached"
            elif state.daily_pnl_pct <= -self.s.max_daily_loss_pct:
                why = "daily loss limit reached"
            else:
                px = self.price_fn(p.symbol) if self.price_fn else None
                if px and p.stop_loss and ((p.direction == "long" and px <= p.stop_loss)
                                           or (p.direction == "short" and px >= p.stop_loss)):
                    why = f"price {px:.2f} is already beyond the stop {p.stop_loss}"
            if why:
                control.mark(self.engine, p.id, "failed", why)
                self.notify(f"{p.symbol}: approved order NOT sent - {why}", "warning")
                results.append(dict(id=p.id, symbol=p.symbol, sent=False, why=why))
                continue
            out = self._execute(control.decision_from_pending(p), p.signal_id, state)
            positions = {k: v for k, v in state.open_positions.items()}
            if out.get("traded"):
                control.mark(self.engine, p.id, "executed", trade_id=out.get("trade_id"))
            else:
                control.mark(self.engine, p.id, "failed", "broker rejected the order", trade_id=out.get("trade_id"))
            results.append(dict(id=p.id, symbol=p.symbol, sent=bool(out.get("traded"))))
        return results


# ------------------------------------------------------------------ scheduler
def _cron_minutes(tf_min: int, offset_min: int = 0) -> str:
    """Minutes past the hour at which a cycle fires: every bar close, plus the data-delay offset."""
    if tf_min >= 60:
        return str(offset_min % 60)
    return ",".join(str((m + offset_min) % 60) for m in range(0, 60, tf_min))


# The frequent jobs only poll for work (requests wait in the DB), so running a few seconds late is harmless.
# Without a grace period APScheduler skips a tick that starts >1s late and we'd alert for nothing.
_LATE_OK_S = 30


def build_scheduler(cycle: TradingCycle):
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    s = cycle.s
    tz = s.timezone
    sched = BlockingScheduler(timezone=tz)
    dow = "mon-fri"
    sh, sm = (int(x) for x in s.session_start.split(":"))
    eh, em = (int(x) for x in s.session_end.split(":"))

    sched.add_job(cycle.start_session, CronTrigger(day_of_week=dow, hour=sh, minute=max(sm - 5, 0), timezone=tz),
                  id="session_start", misfire_grace_time=600)
    sched.add_job(cycle.run_cycle,
                  CronTrigger(day_of_week=dow, hour=f"{sh}-{eh - 1}", minute=_cron_minutes(cycle.tf_min, s.live_delay_minutes),
                              second=s.bar_delay_seconds, timezone=tz),
                  id="cycle", max_instances=1, coalesce=True, misfire_grace_time=120)
    sched.add_job(cycle.process_approvals, IntervalTrigger(seconds=2, timezone=tz), id="approvals",
                  max_instances=1, coalesce=True, misfire_grace_time=_LATE_OK_S)
    sched.add_job(cycle.process_closes, IntervalTrigger(seconds=2, timezone=tz), id="closes",
                  max_instances=1, coalesce=True, misfire_grace_time=_LATE_OK_S)
    sched.add_job(cycle.process_modifies, IntervalTrigger(seconds=2, timezone=tz), id="modifies",
                  max_instances=1, coalesce=True, misfire_grace_time=_LATE_OK_S)
    sched.add_job(cycle.quick_reconcile, IntervalTrigger(seconds=30, timezone=tz), id="reconcile",
                  max_instances=1, coalesce=True, misfire_grace_time=_LATE_OK_S)
    sched.add_job(cycle.maybe_flatten, CronTrigger(day_of_week=dow, hour=f"{sh}-{eh}", minute="*", timezone=tz),
                  id="flatten", max_instances=1, coalesce=True, misfire_grace_time=_LATE_OK_S)
    sched.add_job(cycle.end_session, CronTrigger(day_of_week=dow, hour=eh, minute=em + 2, timezone=tz),
                  id="session_end", misfire_grace_time=1800)
    sched.add_job(lambda: refresh_sentiment(cycle.universe(), engine=cycle.engine),
                  CronTrigger(day_of_week=dow, hour=f"{sh}-{eh - 1}", minute=5, timezone=tz),
                  id="sentiment", max_instances=1, coalesce=True, misfire_grace_time=300)

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
    print(f"[run_loop] mode={mode} broker={cycle.broker.name} tickers={s.tickers} modes={cycle.modes()} tf={s.timeframe}")
    if a.once:
        print(json.dumps(cycle.run_cycle(force=a.force), indent=2, default=str))
        return
    if DiscordApprovals.configured(s):
        bot = DiscordApprovals(s.discord_bot_token, s.discord_channel_id, s.discord_approver_ids, cycle.engine)
        if bot.start():
            cycle.approvals_bot = bot
            print("[run_loop] Discord approval buttons enabled")
    install_stop_handlers()  # before announcing "started", so a stop right after is still reported
    try:
        alerts.log_event("session", f"scheduler process started ({mode})", cycle.engine)
        cycle.notify(f"Scheduler started ({mode}, {cycle.broker.name}).", "info")
        sched = build_scheduler(cycle)
    except SystemExit:  # stopped while starting up
        cycle.notify(f"Scheduler {_STOP_REASON['text']}.", "warning")
        return
    run_until_stopped(sched, cycle)


_STOP_REASON = {"text": "stopped"}


def install_stop_handlers() -> None:
    """Turn kill / launcher Stop / closed terminal into a clean SystemExit so the stop alert can be sent."""
    import signal

    def _on_signal(signum, _frame):
        _STOP_REASON["text"] = f"stopped ({signal.Signals(signum).name})"
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not the main thread / unsupported platform
            pass


def run_until_stopped(sched, cycle) -> None:
    """Run the blocking scheduler and always tell Discord why it ended (Ctrl+C, kill/Stop, or a crash)."""
    reason = _STOP_REASON
    install_stop_handlers()
    try:
        sched.start()
    except KeyboardInterrupt:
        reason["text"] = "stopped (Ctrl+C)"
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001 - report the crash, then let it propagate
        cycle.notify(f"Scheduler crashed: {exc!r}", "error")
        raise
    cycle.notify(f"Scheduler {reason['text']}. Open positions keep their stop/target at the broker.", "warning")


if __name__ == "__main__":
    main()
