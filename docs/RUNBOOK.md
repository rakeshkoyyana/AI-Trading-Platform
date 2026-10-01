# Runbook — from clone to a 2–4 week paper run

All commands run from the repo root with the venv active. Defaults are **paper trading only**.

## 1. One-time setup
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in Alpaca (paper), Finnhub, NewsAPI, Discord keys
python src/smoke_test.py    # prints your Alpaca paper balance
pytest -q                   # ~100 s, fully offline
```

## 2. Data -> signals -> model (run once, then weekly)
```bash
python -m src.data_ingestion.backfill --months 12        # bars for every ticker
python -m src.smc_logic.backfill_signals                  # signals + win/loss labels
python -m src.ml.train                                    # walk-forward eval; saves models/ + MODEL_LOG.md
```
Read `MODEL_LOG.md` honestly: if out-of-sample precision/lift is not better than the raw signal,
**do not trust the filter** — keep `REQUIRE_MODEL=false` and let paper trading collect evidence.

## 3. Validate the SMC port against TradingView (Phase 2 gate)
See `docs/TRADINGVIEW_VALIDATION.md`: add `docs/tradingview_export_patch.pine` to your Pine script, export
3–5 windows to CSV, then `python scripts/validate_against_tradingview.py <csv>` (target >= 90 % core-event agreement).

## 4. Dry run, then go
```bash
python -m src.backtest.replay                             # builds data/demo.db from synthetic data (~75 s)
DATABASE_URL=sqlite:///data/demo.db streamlit run src/dashboard/app.py   # preview the UI offline

python -m src.scheduler.run_loop --once --force --sim     # one full cycle, simulated broker
python -m src.scheduler.run_loop --once --force           # one cycle against Alpaca PAPER (market open for real fills)
python -m src.scheduler.run_loop                          # unattended: 08:30–15:00 CT, Mon–Fri, NYSE calendar
streamlit run src/dashboard/app.py                        # live dashboard (second terminal)
```
The scheduler needs a machine (or small VM) that stays awake and online all session.

## Dashboard guide
`streamlit run src/dashboard/app.py` (auto-refreshes every 30 s; change in the sidebar).
- **Top bar + tape:** paper/live badge, market-window state, kill-switch flag, and a scrolling tape of prices + FinBERT-scored headlines.
- **Chart (TradingView-style):** crosshair with OHLCV/EMA/RSI legend, scroll-zoom/drag-pan, 15m/30m/1H/1D, volume + RSI panes, log scale, magnet crosshair, fullscreen.
  SMC overlays (toggle each): order blocks, FVG, BOS/CHoCH (swing), internal structure, swing levels, premium/discount, EQH/EQL, signals, your trades, SL/TP lines.
  Drawing tools: H-line, trend line, box, measure (Δprice, %, bars, time); drawings persist per symbol in your browser. `Esc` cancels a tool.
- **Match the chart to TradingView:** the sidebar toggle “Include extended-hours bars” (and `INCLUDE_EXTENDED_HOURS` in `.env`) must equal your TradingView chart’s Extended-hours setting.
- **Right rail:** watchlist (price, change, sparkline, sentiment, confluence dots, fresh-signal badge), live news with sentiment chips + “Fetch latest”, and the decision feed (why each signal was taken or blocked).
- **Tabs:** Scanner (confluence screen), Trades, Performance, Decision log, ML model card (shows when the model is *not* validated), System health, Settings (risk limits, kill switch).

## 5. Safety
- **Kill switch:** `touch KILL_SWITCH` blocks all new orders immediately; `rm KILL_SWITCH` re-enables.
- Positions are flattened 5 min before the close; no new entries in the last 15 min.
- Daily loss halt (2 %), max 3 positions, 5 % max position, 0.5 % risk per trade — all env-tunable.
- Live orders require `TRADING_MODE=live` **and** `LIVE_TRADING_CONFIRMED=true`. Don't, until the paper run says so.

## 6. What to review during the paper run
Dashboard -> Trade log + "Why" lines (system events, kind `decision`) for every signal taken or blocked;
compare realised win rate/R:R with `MODEL_LOG.md` expectations; check System health daily (errors, stale data).
Treat results as noise until >= 30–50 closed trades.

## Streamlit Community Cloud
Push to GitHub, create an app pointing at `src/dashboard/app.py`, add secrets as env vars. The dashboard is
read-only, but SQLite lives on the machine running the scheduler — for a cloud dashboard point `DATABASE_URL`
at a hosted DB (e.g. Supabase Postgres) on both sides, or just run the dashboard locally.
