# Project Plan — SMC Trading Agent

This is the detailed, step-by-step build plan. For a flat checklist you can turn into GitHub Issues, see [`../TASKS.md`](../TASKS.md).

## 0. Read this first

- **Robinhood has no official trading API.** The unofficial `robin_stocks` library works today but violates Robinhood's Terms of Service and can trigger account restrictions; it can also break silently on any Robinhood app update. This plan builds execution against **Alpaca** (free, official REST API, real paper trading) first, behind an interface you can later point at Robinhood if you accept that risk.
- This is an engineering roadmap, not investment advice.
- "Free tier" has real limits (API calls/day, DB storage, compute minutes) — noted per tool below.
- Every phase ends with a **"Definition of done"** so you know exactly when to check the box and move on.

---

## 1. Architecture overview

See the diagram and tool table in the top-level [`README.md`](../README.md).

---

## 2. Phase 0 — Environment Setup (Day 1)

**Goal:** a working Python environment, project skeleton, and all accounts/keys ready, before writing any strategy code.

1. **Install Python 3.11.** Check with `python3 --version`.
2. **Create project folder and virtual environment:**
   ```bash
   mkdir smc-trading-agent && cd smc-trading-agent
   python3 -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   ```
3. **Init git + GitHub repo:**
   ```bash
   git init
   git branch -M main
   git remote add origin https://github.com/<your-username>/smc-trading-agent.git
   ```
4. **Create the folder structure** shown in the README.
5. **Create `.gitignore`:**
   ```
   venv/
   .env
   data/
   __pycache__/
   *.pyc
   models/*.pkl
   ```
6. **Create `requirements.txt`:**
   ```
   pandas
   numpy
   yfinance
   alpaca-trade-api
   apscheduler
   scikit-learn
   xgboost
   transformers
   torch
   requests
   python-dotenv
   sqlalchemy
   streamlit
   plotly
   pandas_market_calendars
   pytest
   ```
   Then: `pip install -r requirements.txt`
7. **Create accounts / API keys:**
   - **Alpaca**: alpaca.markets → dashboard → "Generate New Key" under Paper Trading.
   - **Finnhub**: finnhub.io/register → dashboard shows free API key.
   - **NewsAPI** (backup): newsapi.org/register.
   - **Discord webhook**: Server Settings → Integrations → Webhooks → New Webhook.
8. **Create `.env`** (never committed):
   ```
   ALPACA_API_KEY=...
   ALPACA_SECRET_KEY=...
   ALPACA_BASE_URL=https://paper-api.alpaca.markets
   FINNHUB_API_KEY=...
   NEWSAPI_KEY=...
   DISCORD_WEBHOOK_URL=...
   ```
9. **Verify** with `src/smoke_test.py`: loads `.env`, pings Alpaca's `/v2/account`, prints paper balance.

**Definition of done:** `python src/smoke_test.py` prints your Alpaca paper account equity with no errors, and the repo/`.gitignore`/`requirements.txt` are committed to GitHub (`.env` excluded).

---

## 3. Phase 1 — Data Pipeline (Days 2–5)

**Goal:** reliably pull historical and live OHLCV bars into your database.

1. **`src/data_ingestion/alpaca_data.py`** — `get_bars(symbol, timeframe, start, end)` via `alpaca-trade-api`, returns DataFrame with `timestamp, open, high, low, close, volume`.
2. **`src/data_ingestion/yfinance_fallback.py`** — same signature, using `yfinance.download()`.
3. **Database schema** (`src/db/schema.py`, SQLAlchemy):
   - `bars(id, symbol, timestamp, timeframe, open, high, low, close, volume)`
   - `signals(id, symbol, timestamp, signal_type, direction, confirmation_details_json)`
   - `news(id, symbol, timestamp, headline, source, url)`
   - `sentiment_scores(id, news_id, symbol, timestamp, score, label)`
   - `trades(id, symbol, entry_time, exit_time, direction, entry_price, exit_price, qty, pnl, signal_id, model_probability, status)`
   - `model_predictions(id, signal_id, probability, model_version, timestamp)`
4. **`src/data_ingestion/backfill.py`** — pulls 6–12 months of 5m/15m bars for your ticker list into `bars`; add `--incremental` for daily top-ups.
5. **Test** (`tests/test_data_ingestion.py`) — non-empty DataFrame, expected columns, no NaNs in OHLC.

**Definition of done:** `python src/data_ingestion/backfill.py` populates `bars` with months of history for all target tickers; `pytest tests/test_data_ingestion.py` passes.

---

## 4. Phase 2 — Port SMC Logic from Pine Script to Python (Days 6–14)

**Goal:** every condition your Pine Script checks bar-by-bar, replicated in Python, verified against TradingView output.

1. **Inventory pass**: list every condition (order blocks, liquidity sweep, FVG, BOS, CHoCH, premium/discount zone, your triple-confirmation definition) into `docs/smc_logic_spec.md` before writing code.
2. **Port each condition as its own function** in `src/smc_logic/`:
   - `order_blocks.py` → `detect_order_blocks(df)`
   - `fvg.py` → `detect_fvg(df)`
   - `structure.py` → `detect_bos(df)`, `detect_choch(df)`
   - `zones.py` → `detect_premium_discount(df)`
3. **Validate against TradingView**: 3–5 known historical windows, diff Pine Script signals vs. Python output; fix mismatches (most time-consuming part of the project — budget for it).
4. **Composite signal function** — `src/smc_logic/triple_confirmation.py` → `get_signal(df) -> {long, short, none}`.
5. **Backfill signals** across the full historical `bars` table into `signals`.
6. **Label the dataset** — forward return over your holding horizon, win/loss label + raw return, per signal.

**Definition of done:** Python-generated signals match TradingView signals (timestamp + direction) on ≥~90% of instances on your validation windows; `signals` table fully backfilled with labeled outcomes.

---

## 5. Phase 3 — News & Sentiment Pipeline (Days 15–18)

1. **`src/sentiment/news_fetch.py`** — `get_news(symbol, since)` via Finnhub `/company-news`; NewsAPI fallback.
2. **Cache aggressively** — store every article immediately; check DB before re-calling the API.
3. **`src/sentiment/finbert_score.py`** — load `ProsusAI/finbert` once via `transformers.pipeline`; `score_headline(text) -> (label, confidence)`. Download the ~400MB model ahead of time, not inside the live loop.
4. **`src/sentiment/aggregate.py`** — `get_rolling_sentiment(symbol, window)`, recency-weighted rolling average, written to `sentiment_scores`.
5. **Test** on 2–3 tickers for a day; spot-check headlines vs. sentiment labels.

**Definition of done:** each target ticker has a rolling sentiment score updating at least hourly during market hours, stored/queryable, within free-tier API budget.

---

## 6. Phase 4 — ML Model Training (Days 19–26)

**Goal:** a model that predicts P(win) for a triple-confirmation signal — a *filter*, not a standalone signal generator.

1. **Feature matrix** (`src/ml/features.py`): join `signals` + `sentiment_scores` + technical features (ATR, relative volume, time-of-day, day-of-week).
2. **Label**: from Phase 2 (win/loss on forward return threshold).
3. **Walk-forward split** — never random shuffle. Train months 1–4, validate month 5, roll forward.
4. **Baseline model**: `XGBoostClassifier` or `LogisticRegression` predicting P(win); log feature importances as a sanity check.
5. **Evaluate**: precision/recall at a chosen threshold, calibration curve, cumulative return of filtered vs. unfiltered SMC signals.
6. **Save**: `joblib.dump(model, "models/smc_filter_v1.pkl")`; log data range/features/hyperparameters in `models/MODEL_LOG.md`.

**Definition of done:** filtered signal set shows a meaningfully better win rate or risk-adjusted return than unfiltered raw SMC signals on out-of-sample (walk-forward) data; model artifact + metadata saved.

---

## 7. Phase 5 — Decision Engine & Risk Rules (Days 27–29)

1. **`src/decision_engine/engine.py`** — `should_trade(symbol, current_bar_data) -> {trade, direction, size, stop_loss, take_profit}`: (a) SMC triple-confirmation, (b) ML win probability, (c) sentiment gate, (d) hard risk rules.
2. **Hard risk rules** (non-negotiable, independent of the model):
   - Max position size as % of account equity
   - Max daily loss → halt trading for the day
   - Max concurrent open positions
   - Stop-loss/take-profit derived from SMC zones (e.g., beyond the order block)
3. **Paper-mode flag** — `TRADING_MODE=paper` vs `live`, checked by the execution layer before ever routing a live order.

**Definition of done:** `should_trade()` gives correct, explainable decisions on manually-inspected historical bars; paper/live flag wired through to execution.

---

## 8. Phase 6 — Execution Integration (Days 30–33)

1. **`src/execution/base.py`** — abstract interface: `place_order()`, `cancel_order()`, `get_positions()`, `get_account_equity()`.
2. **`src/execution/alpaca_execution.py`** — implements the interface via `alpaca-trade-api`, targeting paper trading by default.
3. **Log every order** (attempt/fill/rejection/partial fill) to `trades` immediately.
4. **Reconciliation check** — each cycle, compare internal state vs. Alpaca's reported state; Discord alert on mismatch.
5. **(Optional, at your own risk) `src/execution/robinhood_execution.py`** via `robin_stocks`, same interface, only invoked behind an explicit config flag, kept isolated from the Alpaca path.

**Definition of done:** a manual test order via your execution module shows up correctly in your Alpaca paper account and is logged in `trades` with matching details.

---

## 9. Phase 7 — Scheduler (Days 34–35)

1. **`src/scheduler/market_hours.py`** — `is_trading_window_now() -> bool` using `pandas_market_calendars` (NYSE) + `zoneinfo` for `America/Chicago`: trading day, and 8:30 AM–3:00 PM CST.
2. **`src/scheduler/run_loop.py`** — APScheduler runs the main cycle (fetch → signals → sentiment → decision engine → execute → log) every N minutes, only while `is_trading_window_now()` is true.
3. **Startup/shutdown hooks** — 8:30 AM CST: log "session started," reset daily loss counters; 3:00 PM CST: log "session ended," optionally flatten positions.
4. **Error handling** — wrap the main cycle in try/except; on exception, Discord alert with traceback, then continue.

**Definition of done:** left running unattended, the scheduler starts at 8:30 AM CST, runs cycles at your chosen interval, and cleanly stops issuing new signals at 3:00 PM CST — verified over at least one full trading day.

---

## 10. Phase 8 — Dashboard (Days 36–40)

1. **`src/dashboard/app.py`** — Streamlit app, read-only DB connection.
2. **KPI cards row**: Net P&L (today/week/month/all-time), win rate %, win/loss $ ratio, trade count, open positions/exposure, rolling Sharpe, max drawdown, latest model confidence.
3. **Equity curve** — Plotly cumulative P&L, shaded paper vs. live periods.
4. **Trade P&L histogram** — distribution of trade outcomes.
5. **Win rate by SMC signal type** — bar chart by confirmation combination.
6. **Win rate/return by sentiment bucket** — bar chart by positive/neutral/negative sentiment at entry.
7. **Price chart with overlays** — candlestick + entry/exit markers + SMC zones (order blocks, FVGs) as shaded rectangles — your main debugging tool.
8. **Trade log table** — sortable/filterable, joined with signal + sentiment detail.
9. **System health panel** — last run, next run, API error count (24h), data staleness, current mode.
10. **Deploy** — push to GitHub, connect at share.streamlit.io, set secrets in Streamlit Cloud's secrets manager.

**Definition of done:** dashboard deployed on a Streamlit Cloud URL, correctly reflects your latest paper trades, updates after each session.

---

## 11. Phase 9 — Paper Trading & Iteration (Days 41–50, ongoing)

1. Run the full pipeline in paper mode for **2–4 weeks of real trading days**, uninterrupted.
2. Daily: check dashboard + Discord alerts, spot-check trades against the price chart overlay.
3. Weekly: compare paper performance vs. backtest/walk-forward predictions; investigate large divergence.
4. Retrain the ML model as new labeled data accumulates; version each retrain in `models/MODEL_LOG.md`.

**Definition of done:** a statistically meaningful paper track record with performance in line with expectations, no unresolved scheduler crashes/errors.

---

## 12. Phase 10 — Go-Live (Day 50+, optional)

1. Review the paper track record with a skeptical pass — small samples are easy to be fooled by.
2. Decide initial live position sizing (start small).
3. Confirm the kill-switch and Discord alerting both work.
4. Flip `TRADING_MODE=live`, monitor closely for the first several sessions.

---

## 13. Timeline estimate

See the top-level [`README.md`](../README.md#timeline-estimate).
