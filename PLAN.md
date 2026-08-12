# Development Plan — AI-Powered SMC Trading Agent

**This file is the actively-maintained source of truth for task tracking**, superseding the flat checklist in `TASKS.md`. Scope and acceptance criteria are defined in `PRD.md`; secrets handling detail lives in `SECRETS_MANAGEMENT_PLAN.md`. Update checkboxes here as work completes.

**Working model:** sequential, one task/phase owned by one person at a time, handed off at each phase's "Definition of done" — not parallel work, since later phases depend directly on earlier outputs.

---

## Phase 0 — Environment Setup ✅ COMPLETE

- [x] Install Python 3.11, create venv
- [x] Init git + GitHub repo (`AI-Trading-Platform`)
- [x] Create folder structure (`src/`, `data/`, `models/`, `tests/`, `notebooks/`) & `.gitignore`
- [x] Create `requirements.txt` & install all packages
- [x] Create Alpaca account, generate paper trading Key ID + Secret
- [x] Create Finnhub account & API key
- [x] Create NewsAPI account & API key
- [x] Set up Discord webhook
- [x] Create local `.env` with all keys
- [x] Write & run `smoke_test.py` — confirmed Alpaca connection (using `alpaca-py`, not the deprecated `alpaca-trade-api`)

**Definition of done:** met. `python src/smoke_test.py` (or `doppler run -- python src/smoke_test.py` post-migration) prints paper account equity with no errors.

---

## Phase 0.5 — Secrets Management Migration (NEW)

Full detail in `SECRETS_MANAGEMENT_PLAN.md`. Added because local `.env` was manually recreated and once pasted in plaintext during setup — not a sustainable or safe pattern for a 2-person team.

- [ ] Create Doppler account + `smc-trading-agent` project + `dev` config
- [ ] Move existing 6 secrets from local `.env` into Doppler
- [ ] Invite teammate to the Doppler project
- [ ] Both teammates: `doppler login` + `doppler setup` locally
- [ ] Add `src/config/secrets.py` (Doppler SDK loader, falls back to `os.getenv` for local override)
- [ ] Update `src/smoke_test.py` to source secrets via Doppler
- [ ] Add `doppler-sdk` to `requirements.txt`
- [ ] Delete local `.env` files once Doppler confirmed working; keep `.env.example`
- [ ] Update `README.md` with the new `doppler run -- <command>` workflow
- [ ] Rotate the Alpaca/Finnhub/NewsAPI/Discord credentials that were previously pasted in plaintext during setup, out of caution

**Definition of done:** both teammates can run `doppler run -- python src/smoke_test.py` successfully on their own machines with zero secrets manually transferred between them.

---

## Phase 1 — Data Pipeline

**Goal:** reliably pull historical and live OHLCV bars into the database.

- [ ] `src/data_ingestion/alpaca_data.py` — `get_bars(symbol, timeframe, start, end) -> DataFrame[timestamp, open, high, low, close, volume]`
- [ ] `src/data_ingestion/yfinance_fallback.py` — same signature, using `yfinance.download()`
- [ ] `src/db/schema.py` (SQLAlchemy) — tables: `bars`, `signals`, `news`, `sentiment_scores`, `trades`, `model_predictions`
- [ ] `src/data_ingestion/backfill.py` — pulls 6–12 months of 5/15-min bars for all target tickers; `--incremental` flag for daily top-ups
- [ ] `tests/test_data_ingestion.py` — non-empty DataFrame, expected columns, no NaNs in OHLC

**Definition of done:** `backfill.py` populates `bars` with months of history for all target tickers; `pytest tests/test_data_ingestion.py` passes.

**Open question to resolve before starting:** which tickers are in scope? (Flag in team sync before this phase begins.)

---

## Phase 2 — Port SMC Logic from Pine Script to Python

**Goal:** every Pine Script condition replicated in Python, verified against TradingView.

- [ ] `docs/smc_logic_spec.md` — full inventory of every condition the Pine Script checks (order blocks, FVG, BOS, CHoCH, premium/discount zones, triple-confirmation definition) — write this **before** any Python
- [ ] `src/smc_logic/order_blocks.py` — `detect_order_blocks(df)`
- [ ] `src/smc_logic/fvg.py` — `detect_fvg(df)`
- [ ] `src/smc_logic/structure.py` — `detect_bos(df)`, `detect_choch(df)`
- [ ] `src/smc_logic/zones.py` — `detect_premium_discount(df)`
- [ ] Validate each function against TradingView on 3–5 historical windows; log and fix mismatches
- [ ] `src/smc_logic/triple_confirmation.py` — `get_signal(df) -> Series[long|short|none]`
- [ ] Backfill `signals` table across full history
- [ ] Label each signal with forward return + win/loss over intended holding horizon

**Definition of done:** ≥90% signal match (timestamp + direction) vs. TradingView on validation windows; `signals` table fully backfilled and labeled.

**Note:** budget the most time here — this is the known bottleneck phase per the original plan.

---

## Phase 3 — News & Sentiment Pipeline

- [ ] `src/sentiment/news_fetch.py` — `get_news(symbol, since)` via Finnhub, NewsAPI fallback
- [ ] Cache fetched articles in `news` table; check DB before re-fetching
- [ ] `src/sentiment/finbert_score.py` — `score_headline(text) -> (label, confidence)` via local FinBERT
- [ ] `src/sentiment/aggregate.py` — `get_rolling_sentiment(symbol, window)`, recency-weighted
- [ ] Manual spot-check of sentiment quality on 2–3 tickers for a day

**Definition of done:** rolling sentiment updates ≥hourly during market hours, stored/queryable, within free-tier quotas.

---

## Phase 4 — ML Model Training

- [ ] `src/ml/features.py` — join signals + sentiment + technicals (ATR, relative volume, time-of-day, day-of-week) into one row per signal
- [ ] Walk-forward train/validation split (never random shuffle)
- [ ] Baseline `XGBoostClassifier` / `LogisticRegression` predicting P(win); log feature importances
- [ ] Evaluate: precision/recall, calibration curve, filtered vs. unfiltered cumulative return
- [ ] Save model (`models/smc_filter_v1.pkl`) + `models/MODEL_LOG.md` entry

**Definition of done:** filtered signals show meaningfully better win rate / risk-adjusted return than unfiltered on walk-forward held-out data.

---

## Phase 5 — Decision Engine & Risk Rules

- [ ] `src/decision_engine/engine.py` — `should_trade(symbol, current_bar_data) -> {trade, direction, size, stop_loss, take_profit}`
- [ ] Hard risk rules: max position size (% equity), max daily loss halt, max concurrent positions, SMC-zone-derived stop/take-profit
- [ ] Paper/live mode config flag (`TRADING_MODE`), sourced from Doppler config (`dev` = paper, `prod` = live)

**Definition of done:** `should_trade()` decisions are explainable on manually-inspected historical bars; paper/live flag wired through to execution.

---

## Phase 6 — Execution Integration

- [ ] `src/execution/base.py` — abstract interface: `place_order`, `cancel_order`, `get_positions`, `get_account_equity`
- [ ] `src/execution/alpaca_execution.py` — implements interface via `alpaca-py`, paper by default
- [ ] Log every order attempt/fill/rejection to `trades` table immediately
- [ ] Reconciliation check each cycle: internal state vs. Alpaca-reported state; Discord alert on mismatch
- [ ] (Optional) `src/execution/robinhood_execution.py` via `robin_stocks`, isolated behind a config flag

**Definition of done:** manual test order appears correctly in Alpaca paper account and is logged in `trades`.

---

## Phase 7 — Scheduler

- [ ] `src/scheduler/market_hours.py` — `is_trading_window_now()`, timezone-aware (America/Chicago), market-day-aware via `pandas_market_calendars`
- [ ] `src/scheduler/run_loop.py` — APScheduler main cycle (fetch → signals → sentiment → decision → execute → log)
- [ ] Session start/end hooks (reset counters at open; log/flatten at close per strategy)
- [ ] Error handling: try/except around main cycle, Discord alert with traceback on exception, continue rather than crash

**Definition of done:** runs unattended, starts at 8:30am CST, executes cycles at chosen interval, stops cleanly at 3:00pm CST — verified over ≥1 full trading day.

---

## Phase 8 — Dashboard

- [ ] `src/dashboard/app.py` — Streamlit app, read-only DB connection
- [ ] KPI cards: net P&L, win rate, win/loss ratio, trade count, open exposure, rolling Sharpe, max drawdown, latest model confidence
- [ ] Equity curve (Plotly), shaded paper vs. live periods
- [ ] Trade P&L histogram
- [ ] Win rate by SMC signal type (bar chart)
- [ ] Win rate / return by sentiment bucket (bar chart)
- [ ] Price chart with entry/exit markers + SMC zone overlays
- [ ] Sortable/filterable trade log table
- [ ] System health panel (last run, next run, API error count, data staleness, current mode)
- [ ] Deploy to Streamlit Community Cloud, secrets sourced from Doppler service token (not a committed `.env`)

**Definition of done:** deployed dashboard reflects latest paper trades, updates after each session.

---

## Phase 9 — Paper Trading & Iteration

- [ ] Run full pipeline in paper mode, smoke test
- [ ] Continuous paper trading for 2–4 weeks
- [ ] Weekly comparison: live paper results vs. backtest expectations
- [ ] Retrain ML model as new labeled data accumulates; version in `MODEL_LOG.md`
- [ ] Fix bugs / tune risk parameters found during paper trading

**Definition of done:** statistically meaningful paper track record, performance in line with expectations, no unresolved scheduler errors.

---

## Phase 10 — Go-Live (optional, gated)

- [ ] Review paper track record with a skeptical pass (small-sample-size awareness)
- [ ] Decide initial live position sizing (start small)
- [ ] Confirm kill-switch + Discord alerting both work
- [ ] Create separate `prod` config in Doppler, isolated from `dev`
- [ ] Flip `TRADING_MODE=live`, monitor closely for first several sessions

**Definition of done:** joint go/no-go decision made by both teammates; not a unilateral flip.

---

## Ownership log

Track who owns each phase as it's picked up — keep this updated so handoffs are unambiguous.

| Phase | Owner | Status | Notes |
|---|---|---|---|
| 0 | Rakesh | Complete | |
| 0.5 (Secrets) | TBD | Not started | |
| 1 | TBD | Not started | |
| 2 | TBD | Not started | |
| 3–10 | TBD | Not started | |
