#!/usr/bin/env bash
#
# create_github_issues.sh
#
# Creates one GitHub Milestone per project phase and one Issue per task
# (from TASKS.md) in the current repo, so you get an instant, trackable
# backlog you can drop onto a GitHub Project board.
#
# Requirements:
#   - GitHub CLI installed and authenticated: https://cli.github.com/  (`gh auth login`)
#   - Run this from inside a cloned copy of the repo (or pass --repo owner/name)
#
# Usage:
#   chmod +x scripts/create_github_issues.sh
#   ./scripts/create_github_issues.sh                 # uses the repo in the current directory
#   ./scripts/create_github_issues.sh --repo you/smc-trading-agent   # or target explicitly
#
# Safe to re-run: gh will just create duplicate issues if you run it twice,
# so only run it once per repo (or delete the milestones/issues first).

set -euo pipefail

REPO_ARG=()
if [[ "${1:-}" == "--repo" && -n "${2:-}" ]]; then
  REPO_ARG=(--repo "$2")
fi

if ! command -v gh &> /dev/null; then
  echo "GitHub CLI ('gh') not found. Install it from https://cli.github.com/ and run 'gh auth login' first." >&2
  exit 1
fi

# phase_key|Milestone title|Label
PHASES=(
  "phase0|Phase 0 – Environment Setup|phase-0"
  "phase1|Phase 1 – Data Pipeline|phase-1"
  "phase2|Phase 2 – SMC Logic Port|phase-2"
  "phase3|Phase 3 – News & Sentiment|phase-3"
  "phase4|Phase 4 – ML Model|phase-4"
  "phase5|Phase 5 – Decision Engine & Risk|phase-5"
  "phase6|Phase 6 – Execution Integration|phase-6"
  "phase7|Phase 7 – Scheduler|phase-7"
  "phase8|Phase 8 – Dashboard|phase-8"
  "phase9|Phase 9 – Paper Trading & Iteration|phase-9"
  "phase10|Phase 10 – Go-Live|phase-10"
)

# phase_key|Task title|Task description
TASKS=(
  "phase0|Install Python & create venv|Install Python 3.11, create smc-trading-agent/ folder, run python3 -m venv venv, activate it."
  "phase0|Init git + GitHub repo|git init, create private GitHub repo, add remote, first commit."
  "phase0|Create folder structure & .gitignore|Set up src/, data/, models/, tests/, notebooks/; create .gitignore excluding venv/, .env, data/."
  "phase0|Create requirements.txt & install|Add all needed packages, run pip install -r requirements.txt."
  "phase0|Create Alpaca account & get paper API keys|Sign up at alpaca.markets, generate paper trading Key ID + Secret."
  "phase0|Create Finnhub account & get API key|Sign up at finnhub.io/register, copy free-tier API key."
  "phase0|Create NewsAPI account (backup) & get API key|Sign up at newsapi.org/register."
  "phase0|Set up Discord webhook for alerts|Create webhook URL in a Discord server for error/status alerts."
  "phase0|Create .env file with all keys|Store Alpaca, Finnhub, NewsAPI keys and Discord webhook URL; confirm it's gitignored."
  "phase0|Write and run smoke_test.py|Script that loads .env and prints Alpaca paper account balance to confirm setup works end-to-end."
  "phase1|Build Alpaca data ingestion function|get_bars(symbol, timeframe, start, end) returning a clean OHLCV DataFrame."
  "phase1|Build yfinance fallback ingestion function|Same signature/output as Alpaca version, for redundancy."
  "phase1|Design and create database schema|Tables: bars, signals, news, sentiment_scores, trades, model_predictions (SQLAlchemy)."
  "phase1|Build historical backfill script|Pulls 6-12 months of intraday bars for all target tickers into the bars table."
  "phase1|Write data ingestion tests|Assert non-empty DataFrame, correct columns, no NaNs in OHLC for a sample pull."
  "phase2|Write full SMC condition inventory doc|List every rule in the Pine Script (order blocks, FVG, BOS, CHoCH, zones, triple-confirmation definition) in docs/smc_logic_spec.md."
  "phase2|Port order block detection to Python|Function that adds order-block columns to the OHLCV DataFrame."
  "phase2|Port fair value gap (FVG) detection to Python|Function that flags FVGs with high/low bounds."
  "phase2|Port break of structure (BOS) detection to Python|Function flagging BOS events."
  "phase2|Port change of character (CHoCH) detection to Python|Function flagging CHoCH events."
  "phase2|Port premium/discount zone logic to Python|Function classifying price location within a range."
  "phase2|Validate against TradingView on 3-5 historical windows|Manually compare Python output vs. Pine Script signals; log and fix mismatches."
  "phase2|Build composite triple-confirmation signal function|Combines the three chosen confirmations exactly as the Pine Script does."
  "phase2|Backfill signals table across full history|Run composite function over all historical bars."
  "phase2|Label signals with forward returns / win-loss|Compute outcome over the intended holding horizon for every signal."
  "phase3|Build Finnhub news fetch function|get_news(symbol, since), with NewsAPI fallback."
  "phase3|Add caching to avoid redundant API calls|Check DB before re-fetching articles already stored."
  "phase3|Set up FinBERT sentiment scoring locally|Load ProsusAI/finbert via transformers, function to score a headline."
  "phase3|Build rolling sentiment aggregation function|Rolling window average sentiment per symbol, recency-weighted."
  "phase3|Spot-check sentiment output quality|Manually review a sample of headlines vs. assigned sentiment for sanity."
  "phase4|Build feature matrix from signals + sentiment + technicals|One row per signal, joined features."
  "phase4|Implement walk-forward train/validation split|No random shuffling; roll forward in time."
  "phase4|Train baseline XGBoost/logistic regression classifier|Predict P(win) per signal."
  "phase4|Evaluate model: precision/recall, calibration, filtered vs unfiltered returns|Confirm the model adds value over raw SMC signals."
  "phase4|Save trained model artifact + log metadata|models/smc_filter_v1.pkl + entry in MODEL_LOG.md."
  "phase5|Build should_trade() decision function|Combines SMC signal + ML probability + sentiment gate."
  "phase5|Implement max position size rule|Cap position size as a % of account equity."
  "phase5|Implement daily loss circuit breaker|Halt trading for the day once a max daily loss is hit."
  "phase5|Implement stop-loss / take-profit logic tied to SMC zones|Derive stop/target from order block/FVG levels."
  "phase5|Add paper/live mode config flag|TRADING_MODE=paper vs live, checked before any live order."
  "phase6|Build broker-agnostic execution interface|Abstract place_order, cancel_order, get_positions, get_account_equity."
  "phase6|Implement Alpaca execution adapter|Concrete implementation targeting paper trading by default."
  "phase6|Log every order attempt/fill/rejection to trades table|Persist broker responses immediately."
  "phase6|Build position reconciliation check|Compare internal state vs. broker-reported state each cycle."
  "phase6|(Optional) Build Robinhood adapter via robin_stocks|Behind a config flag, isolated from the Alpaca path."
  "phase7|Build is_trading_window_now() function|Timezone-aware (America/Chicago), 8:30 AM-3:00 PM CST, market-day aware via pandas_market_calendars."
  "phase7|Build main scheduled run loop with APScheduler|Runs the full cycle on an interval during the trading window."
  "phase7|Add session start/end hooks|Reset daily counters at open; log/flatten at close per your strategy."
  "phase7|Add error handling + Discord alerting on exceptions|Wrap the main cycle so one bad run doesn't crash the day."
  "phase8|Set up Streamlit app skeleton|Read-only DB connection, base layout."
  "phase8|Build KPI summary cards|Net P&L, win rate, win/loss ratio, trade count, exposure, Sharpe, drawdown, model confidence."
  "phase8|Build equity curve chart|Plotly cumulative P&L, shaded paper vs live."
  "phase8|Build trade P&L distribution chart|Histogram of individual trade outcomes."
  "phase8|Build win-rate-by-signal-type chart|Bar chart grouped by confirmation combination."
  "phase8|Build win-rate-by-sentiment chart|Bar chart grouped by sentiment bucket at entry."
  "phase8|Build price chart with entry/exit + SMC zone overlays|Candlestick with markers and shaded order block/FVG zones."
  "phase8|Build sortable/filterable trade log table|st.dataframe joined with signal and sentiment detail."
  "phase8|Build system health panel|Last run, next run, API error count, data staleness, current mode."
  "phase8|Deploy dashboard to Streamlit Community Cloud|Connect repo at share.streamlit.io, set secrets."
  "phase9|Run full pipeline in paper mode, smoke test|End-to-end dry run before continuous operation."
  "phase9|Run continuous paper trading for 2-4 weeks|Uninterrupted, scheduler running daily."
  "phase9|Weekly comparison: live paper results vs. backtest expectations|Investigate large divergence."
  "phase9|Retrain ML model with newly accumulated data|Version each retrain in models/MODEL_LOG.md."
  "phase9|Fix bugs / tune risk parameters found during paper trading|Ongoing hardening pass."
  "phase10|Review paper track record for statistical significance|Skeptical pass on sample size and results."
  "phase10|Decide initial live position sizing|Start small relative to what risk rules technically allow."
  "phase10|Confirm kill-switch and alerts work correctly|Verify the halt mechanism and Discord alerting end-to-end."
  "phase10|Enable live trading with small size|Flip TRADING_MODE=live, monitor closely."
)

echo "Creating milestones and labels..."
declare -A MILESTONE_TITLE
declare -A PHASE_LABEL
for entry in "${PHASES[@]}"; do
  IFS='|' read -r key title label <<< "$entry"
  MILESTONE_TITLE["$key"]="$title"
  PHASE_LABEL["$key"]="$label"
  gh api "${REPO_ARG[@]}" -X POST /repos/{owner}/{repo}/milestones -f title="$title" \
    > /dev/null 2>&1 || echo "  (milestone '$title' may already exist, continuing)"
  gh label create "$label" "${REPO_ARG[@]}" --color "0e8a16" --force > /dev/null 2>&1 || true
done

echo "Creating issues..."
for entry in "${TASKS[@]}"; do
  IFS='|' read -r key title desc <<< "$entry"
  milestone="${MILESTONE_TITLE[$key]}"
  label="${PHASE_LABEL[$key]}"
  gh issue create "${REPO_ARG[@]}" \
    --title "$title" \
    --body "$desc" \
    --label "$label" \
    --milestone "$milestone" > /dev/null
  echo "  created: [$milestone] $title"
done

echo "Done. Open your repo's Issues tab, select all, and add them to your Project board."
