# Task Checklist

Every task below maps 1:1 to an issue created by `scripts/create_github_issues.sh` (milestone = phase). Check items off here for a quick local view, or work from the Issues/Project board on GitHub.

## Phase 0 – Environment Setup
- [ ] **Install Python & create venv** — Install Python 3.11, create `smc-trading-agent/` folder, run `python3 -m venv venv`, activate it.
- [ ] **Init git + GitHub repo** — `git init`, create private GitHub repo, add remote, first commit.
- [ ] **Create folder structure & .gitignore** — Set up `src/`, `data/`, `models/`, `tests/`, `notebooks/`; create `.gitignore` excluding `venv/`, `.env`, `data/`.
- [ ] **Create requirements.txt & install** — Add all needed packages, run `pip install -r requirements.txt`.
- [x] **Create Alpaca account & get paper API keys** — Sign up at alpaca.markets, generate paper trading Key ID + Secret.
- [x] **Create Finnhub account & get API key** — Sign up at finnhub.io/register, copy free-tier API key.
- [x] **Create NewsAPI account (backup) & get API key** — Sign up at newsapi.org/register.
- [x] **Set up Discord webhook for alerts** — Create webhook URL in a Discord server for error/status alerts.
- [x] **Create .env file with all keys** — Store Alpaca, Finnhub, NewsAPI keys and Discord webhook URL; confirm it's gitignored.
- [x] **Write and run smoke_test.py** — Script that loads .env and prints Alpaca paper account balance to confirm setup works end-to-end.

## Phase 1 – Data Pipeline
- [ ] **Build Alpaca data ingestion function** — `get_bars(symbol, timeframe, start, end)` returning a clean OHLCV DataFrame.
- [ ] **Build yfinance fallback ingestion function** — Same signature/output as Alpaca version, for redundancy.
- [ ] **Design and create database schema** — Tables: bars, signals, news, sentiment_scores, trades, model_predictions (SQLAlchemy).
- [ ] **Build historical backfill script** — Pulls 6–12 months of intraday bars for all target tickers into the `bars` table.
- [ ] **Write data ingestion tests** — Assert non-empty DataFrame, correct columns, no NaNs in OHLC for a sample pull.

## Phase 2 – SMC Logic Port
- [ ] **Write full SMC condition inventory doc** — List every rule in the Pine Script (order blocks, FVG, BOS, CHoCH, zones, triple-confirmation definition) in `docs/smc_logic_spec.md`.
- [ ] **Port order block detection to Python** — Function that adds order-block columns to the OHLCV DataFrame.
- [ ] **Port fair value gap (FVG) detection to Python** — Function that flags FVGs with high/low bounds.
- [ ] **Port break of structure (BOS) detection to Python** — Function flagging BOS events.
- [ ] **Port change of character (CHoCH) detection to Python** — Function flagging CHoCH events.
- [ ] **Port premium/discount zone logic to Python** — Function classifying price location within a range.
- [ ] **Validate against TradingView on 3–5 historical windows** — Manually compare Python output vs. Pine Script signals; log and fix mismatches.
- [ ] **Build composite triple-confirmation signal function** — Combines the three chosen confirmations exactly as the Pine Script does.
- [ ] **Backfill signals table across full history** — Run composite function over all historical bars.
- [ ] **Label signals with forward returns / win-loss** — Compute outcome over the intended holding horizon for every signal.

## Phase 3 – News & Sentiment
- [ ] **Build Finnhub news fetch function** — `get_news(symbol, since)`, with NewsAPI fallback.
- [ ] **Add caching to avoid redundant API calls** — Check DB before re-fetching articles already stored.
- [ ] **Set up FinBERT sentiment scoring locally** — Load `ProsusAI/finbert` via `transformers`, function to score a headline.
- [ ] **Build rolling sentiment aggregation function** — Rolling window average sentiment per symbol, recency-weighted.
- [ ] **Spot-check sentiment output quality** — Manually review a sample of headlines vs. assigned sentiment for sanity.

## Phase 4 – ML Model
- [ ] **Build feature matrix from signals + sentiment + technicals** — One row per signal, joined features.
- [ ] **Implement walk-forward train/validation split** — No random shuffling; roll forward in time.
- [ ] **Train baseline XGBoost/logistic regression classifier** — Predict P(win) per signal.
- [ ] **Evaluate model: precision/recall, calibration, filtered vs unfiltered returns** — Confirm the model adds value over raw SMC signals.
- [ ] **Save trained model artifact + log metadata** — `models/smc_filter_v1.pkl` + entry in `MODEL_LOG.md`.

## Phase 5 – Decision Engine & Risk
- [ ] **Build should_trade() decision function** — Combines SMC signal + ML probability + sentiment gate.
- [ ] **Implement max position size rule**
- [ ] **Implement daily loss circuit breaker**
- [ ] **Implement stop-loss / take-profit logic tied to SMC zones**
- [ ] **Add paper/live mode config flag**

## Phase 6 – Execution Integration
- [ ] **Build broker-agnostic execution interface** — Abstract `place_order`, `cancel_order`, `get_positions`, `get_account_equity`.
- [ ] **Implement Alpaca execution adapter**
- [ ] **Log every order attempt/fill/rejection to trades table**
- [ ] **Build position reconciliation check** — Compare internal state vs. broker-reported state each cycle.
- [ ] **(Optional) Build Robinhood adapter via robin_stocks** — Behind a config flag, isolated from the Alpaca path.

## Phase 7 – Scheduler
- [ ] **Build is_trading_window_now() function** — Timezone-aware (America/Chicago), 8:30 AM–3:00 PM CST, market-day aware via pandas_market_calendars.
- [ ] **Build main scheduled run loop with APScheduler**
- [ ] **Add session start/end hooks** — Reset daily counters at open; log/flatten at close per your strategy.
- [ ] **Add error handling + Discord alerting on exceptions**

## Phase 8 – Dashboard
- [ ] **Set up Streamlit app skeleton**
- [ ] **Build KPI summary cards**
- [ ] **Build equity curve chart**
- [ ] **Build trade P&L distribution chart**
- [ ] **Build win-rate-by-signal-type chart**
- [ ] **Build win-rate-by-sentiment chart**
- [ ] **Build price chart with entry/exit + SMC zone overlays**
- [ ] **Build sortable/filterable trade log table**
- [ ] **Build system health panel**
- [ ] **Deploy dashboard to Streamlit Community Cloud**

## Phase 9 – Paper Trading & Iteration
- [ ] **Run full pipeline in paper mode, smoke test**
- [ ] **Run continuous paper trading for 2–4 weeks**
- [ ] **Weekly comparison: live paper results vs. backtest expectations**
- [ ] **Retrain ML model with newly accumulated data**
- [ ] **Fix bugs / tune risk parameters found during paper trading**

## Phase 10 – Go-Live
- [ ] **Review paper track record for statistical significance**
- [ ] **Decide initial live position sizing**
- [ ] **Confirm kill-switch and alerts work correctly**
- [ ] **Enable live trading with small size**
