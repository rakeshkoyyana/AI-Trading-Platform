# AlphaWave runbook — from clone to a 2–4 week paper run

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
python -m src.data_ingestion.backfill --months 12        # bars for every trade ticker (SIP feed, see below)
python -m src.data_ingestion.backfill --timeframe 5Min --months 2   # optional: only the dashboard's 5m chart needs these
python -m src.smc_logic.backfill_signals                  # signals + win/loss labels
python -m src.ml.train                                    # walk-forward eval; saves models/ + MODEL_LOG.md
```
Read `MODEL_LOG.md` honestly: if out-of-sample precision/lift is not better than the raw signal,
**do not trust the filter** — keep `REQUIRE_MODEL=false` and let paper trading collect evidence.

### Data feed: SIP (default) vs IEX
`ALPACA_DATA_FEED=sip` pulls the **consolidated tape** (all US exchanges): the same prices and volume TradingView shows,
and the full pre/post-market session, so signals line up with a TradingView chart. IEX is one exchange only (about 2 % of the
volume, and most extended-hours bars are missing), which makes the volume-spike confirmation fire differently.

The free Alpaca plan serves SIP only for data **older than 15 minutes**, so `SIP_DELAY_MINUTES=16` is applied everywhere:
the scheduler fires 16 minutes after each bar closes (:16, :31, :46, :01), uses only bars inside that horizon, and a signal
therefore reaches the broker about 16 minutes after its bar closed (the paper trade log shows the cost: compare each
fill with the signal bar's close). With a real-time SIP subscription set `SIP_DELAY_MINUTES=0`. `ALPACA_DATA_FEED=iex`
restores real-time single-exchange data. Re-running `backfill` **overwrites** stored bars, so switching feeds is
`backfill` -> `backfill_signals` (drops signals the new bars no longer produce) -> `train`.

### Live candles without the delay (free hybrid, `LIVE_HYBRID=true`, default)
Exact SIP bars arrive 15 minutes late, so the newest minutes are **estimated** from the real-time IEX feed: IEX prices (they track
the consolidated tape closely on liquid names) with IEX volume multiplied by `k = median(SIP volume / IEX volume)` measured on the
last ~60 overlapping regular-hours bars. The scheduler then fires right at each bar close (`:00 :15 :30 :45` + `BAR_DELAY_SECONDS`)
and decides on the just-closed candle. Estimated bars are never stored, are drawn faded with a `live est.` tag, and are replaced by
the exact SIP bar later. Each cycle logs `live_tail` events (bars added, k, price error, volume-spike agreement) to System health;
if IEX fails the cycle falls back to the delayed SIP bars. For exact real-time use Alpaca's paid plan with `SIP_DELAY_MINUTES=0`
and `LIVE_HYBRID=false`.

### Which tickers trade today: Off / Ask / Auto
The **Trade control** panel (under the KPI cards) sets a mode per ticker. `.env` `TICKERS` start at `DEFAULT_TRADE_MODE` (default
`ask`); watchlist and searched symbols start **Off** but can be set to Ask or Auto. **Off**: never opens a position. **Ask**: a
qualifying signal waits for your Approve / Reject for `APPROVAL_TTL_MINUTES` (default 10) and a Discord message tells you. **Auto**:
sent to Alpaca immediately. The scheduler's approvals job (every 10 s) re-checks the trading window, position limits, daily-loss
limit and that price has not already crossed the stop before it sends an approved order. Run `python -m src.scheduler.run_loop`
for approvals to execute.

### Shadow council (free, advisory)
Every fresh signal also gets rule-based analyst votes (momentum room before the RSI exit, volume, structure, premium/discount,
order block/FVG, higher timeframes, sentiment). They are stored in `council_votes` next to what the engine actually did and are
shown on Ask cards, but they **never change a decision**. The **Council (shadow)** tab grades them: win rate for
agree / mixed / disagree and per analyst, from the paper-run log and (as an instant sanity check) from stored labelled history.
After 2–4 weeks, promote it to a real filter only if agreement clearly predicts wins (verdict thresholds are fixed in advance
in `src/decision_engine/council.py`). An LLM debate (bull/bear, news/fundamentals analysts) is parked for later because it costs money.

### Exits: Pine rules + protective stop (`EXIT_MODE=pine`)
Entries carry only a broker-side **protective stop** (OTO order). Positions are closed by the Pine strategy's own rules on the first
closed bar where `RSI ≥ 70` (long) / `RSI ≤ 30` (short) or the EMA 9/21 trend flips against the position, which is also what the
purple fills on the chart show. That runs for every mode (even Off) and a flip is followed by a new entry only as the ticker's mode
allows. `EXIT_MODE=bracket` restores the fixed stop + target bracket.

### Any ticker, loaded on demand
The dashboard search box covers every exchange-listed US stock/ETF (symbol list from Alpaca, cached a day). Opening a symbol
pulls its history once (12 months of 15m, 2 months of 5m) and later only tops up the newest bars; star it to keep it in
`data/watchlist.json` (max 12). Research symbols are Off by default: switch one to Ask or Auto in Trade control to let the scheduler consider it.

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
- **Chart (TradingView-style):** crosshair with OHLCV/EMA/RSI legend, **Strategy fills** (the Pine strategy’s own orders: Long = blue arrow up, Short = red arrow down, purple closes when RSI reaches 70/30 or the EMA trend flips, reversals; captions appear when zoomed in), scroll-zoom/drag-pan, 5m/15m/30m/1H/2H/4H/1D (each timeframe computes its own signals and SMC zones, like a TradingView chart of that timeframe; 2H/4H/1H buckets follow the sessions: 09:30 regular, 04:00 pre-market, 16:00 after-hours), volume + RSI panes, log scale, magnet crosshair, fullscreen.
  Order labels follow TradingView (`Long +17`, `Short -17`, reversal `+34`, `Close Long -17`): sized 10 % of equity from the script's $10,000 over the loaded history, so they match TradingView only if your chart starts at the same bar. **PDH/PDL/PWH/PWL** draw the previous day/week high-low; EMA 9 is blue, EMA 21 red.
  SMC overlays (toggle each): order blocks, FVG, BOS/CHoCH (swing), internal structure, swing levels, premium/discount, EQH/EQL, signals, your trades, SL/TP lines.
  Drawing tools: H-line, trend line, box, measure (Δprice, %, bars, time); drawings persist per symbol in your browser. `Esc` cancels a tool.
- **Match the chart to TradingView:** the sidebar toggle “Include extended-hours bars” (and `INCLUDE_EXTENDED_HOURS` in `.env`) must equal your TradingView chart’s Extended-hours setting.
- **Trade control:** per-ticker Off / Ask / Auto and the approval queue (see above).
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
