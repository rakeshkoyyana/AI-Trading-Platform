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
for approvals to execute. A pulsing banner at the top of the dashboard and a short chime announce each new request (toggle the chime in the sidebar; browsers only play sound after you have clicked on the page once). Discord gets the same message as a phone push for when the dashboard is closed.

### Shadow council (free, advisory)
Every fresh signal also gets rule-based analyst votes (momentum room before the RSI exit, volume, structure, premium/discount,
order block/FVG, higher timeframes, sentiment). They are stored in `council_votes` next to what the engine actually did and are
shown on Ask cards, but they **never change a decision**. The **Council (shadow)** tab grades them: win rate for
agree / mixed / disagree and per analyst, from the paper-run log and (as an instant sanity check) from stored labelled history.
After 2–4 weeks, promote it to a real filter only if agreement clearly predicts wins (verdict thresholds are fixed in advance
in `src/decision_engine/council.py`). An LLM debate (bull/bear, news/fundamentals analysts) is parked for later because it costs money.

### Exits: stop, 1:2 take-profit and Pine rules (`EXIT_MODE=hybrid`, the default)
**Stop:** just beyond the nearest protective SMC level (bullish order-block low, else swing low; mirrored for shorts) plus a 0.1 ATR
buffer, else 1.5 ATR. A setup is skipped if the stop is wider than `MAX_STOP_PCT` (5% of price) or tighter than `MIN_STOP_ATR` (0.25 ATR).
Size = 0.5% of equity risked / stop distance, capped at 5% of equity.
**Target:** `TARGET_RR` x the stop distance (default 2.0, i.e. 1:2), sent to the broker with the stop as a bracket (whichever fills first cancels the other).
**Pine exits stay on:** the first closed bar where `RSI ≥ 70` (long) / `RSI ≤ 30` (short) or the EMA 9/21 trend flips against the position closes it
early and cancels the bracket. That runs for every mode (even Off).
Other values: `EXIT_MODE=pine` = stop + Pine exits only; `EXIT_MODE=bracket` = stop + structural target only.

**Speed and safety:** the stop (and target) are sent to Alpaca *with* the entry and rest at the broker, so they trigger and fill at the
exchange with no help from this app. The scheduler books stop/target fills every 30 s, picks up approvals and Close clicks every 2 s, and
warns (Discord + dashboard events) if an open position ever has no resting stop. The dashboard redraws by itself whenever a trade,
approval or close changes; no manual refresh.

**Drag the lines (like TradingView's position tool):** a trade waiting for approval and every open position is drawn on the chart
with its entry, a red stop line and a green target line (labels show the $ risk, $ reward and R:R). Grab a line and drag it.
- *Waiting for approval:* drag SL / TP, then **✔ Confirm trade** on the chart: your levels replace the proposed ones and the trade is
  approved in one step (**✖ Reject** declines it). The Approve button below uses your edited levels too.
- *Open position:* drag, then **✔ Apply to broker**; the scheduler moves the resting stop / take-profit orders within ~2 s.
  Nothing is sent just by dragging. A stop may be tightened freely but widened to at most 2x its original distance, must stay on the loss side
  of the market price, and within `MAX_STOP_PCT`. A trade opened without a take-profit order (`EXIT_MODE=pine`) only has a draggable stop.

**Close button:** Trade control lists open positions; **Close SYMBOL** then **Confirm** queues a market close. The scheduler sends it within ~2 s
(only while the market is open; a request it does not pick up within 2 minutes expires so it can't fire later by surprise).

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

### Your trade list, position size, trade details, About
- **Trade list**: Trade control shows only the tickers you added. The first run seeds it from `TICKERS` in `.env` (plus anything you had already set to Ask/Auto); after that `.env` is ignored for this: use **✕ Remove** on a ticker, or **Add a ticker to trade** (search box) to change it. New tickers start **Off**. Removing is refused while a position is open. The scheduler follows the same list. Searching a ticker only opens its chart (with a **＋ Add to trade list** button); no chart opens until you click a ticker.

- **Position size** (Trade control → "Position size"): edit the max position (% of equity) and risk per trade (%). Shares = the smallest of risk-based size, the position cap and buying power; with tight stops the cap binds, so raise it for bigger positions. Saved values apply from the next signal without a restart (the scheduler reads them every cycle); "Reset to .env" goes back to the defaults. Each approval card also has a **Shares** box so you can change one trade's size before approving (one edit may raise it up to 3x).
- **Trade details**: click a row in Recent trades for a popup with P&L and R, stop/target, money at risk, why it was taken, signal details, stop/target changes and the event log.
- **About AlphaWave** is hidden until you click the AlphaWave logo in the top bar, and has a Close button. It shows the About section at the bottom of the dashboard: the story, the idea, an animated architecture map, one trade's life with a sizing calculator, the guardrails and the road ahead. It is a static page (`src/dashboard/static/about.html`), so edit the wording there. The lab uses invented prices and the progress bar assumes a 28-day paper run from Oct 2.

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

## Desktop app (macOS, no terminal)

Run once: `bash scripts/install_desktop_app.sh` — puts one **AlphaWave** icon (with the logo) on your Desktop.

Double-click it any time. It:
1. Pulls the latest `main` (only if you're on a clean `main`; otherwise it skips the update).
2. Starts the scheduler and dashboard if they aren't running.
3. **Restarts both if new code arrived** (so merged features show up). If nothing changed and it's already running, a dialog asks **Open dashboard / Restart / Stop**.
4. Opens http://localhost:8501.

So after a merge: just double-click AlphaWave again. Logs: `logs/scheduler.log`, `logs/dashboard.log`, `logs/update.log`.

- Dialogs and banners come from the AlphaWave app itself (so they show the logo). The first time, macOS may ask to allow notifications for AlphaWave: allow it (System Settings > Notifications > AlphaWave).
- Each launch writes a phase-by-phase timing to `logs/launcher-timing.log` (pull, environment, old dashboard stopped, dashboard answering, scheduler restarted). If a launch feels slow, that file shows which step took the time.
- It does not run `caffeinate`; keep your Mac awake yourself.
- Starting/restarting only affects the local processes. It never closes positions or orders at the broker.
- To stop: double-click AlphaWave and choose **Stop** (closing the browser tab does not stop anything). Terminal alternative: `bash scripts/alphawave_stop.sh`.
- If the icon does nothing, check `~/Library/Logs/AlphaWave-app.log` and `logs/dashboard.log`. The installer also runs a setup check (Python env) and prints any problem.
- First launch: if macOS blocks it, right-click the app > Open once. Needs a working `venv` (or `.venv`).
- Re-run the installer only if you move the repo. It also removes the old "Stop AlphaWave" icon if you installed an earlier version.

**Discord stop alert:** the scheduler posts "Scheduler stopped (SIGTERM / Ctrl+C)" when it is stopped (including via the AlphaWave Stop choice) and "Scheduler crashed: ..." on an unexpected error. A hard kill, power loss or Mac sleep cannot send an alert.

## Approve from Discord (optional, free)

When a trade is waiting for approval and you're away from the dashboard, Discord shows the proposal with **Approve** and **Reject** buttons. A click does exactly what the dashboard buttons do (the scheduler re-checks everything and sends the order). The dashboard still works too, and whichever you use first wins.

One-time setup (about 5 minutes):
1. discord.com/developers/applications > **New Application** (name it AlphaWave) > **Bot** > **Reset Token** > copy it.
2. **OAuth2 > URL Generator**: tick scope `bot`, permissions *View Channels* and *Send Messages*. Open the URL and add the bot to your server. No privileged intents are needed.
3. In Discord: Settings > Advanced > **Developer Mode** on. Right-click your alerts channel > **Copy Channel ID**. Right-click your own name > **Copy User ID**.
4. Add to `.env`:
   ```
   DISCORD_BOT_TOKEN=...
   DISCORD_CHANNEL_ID=...
   DISCORD_APPROVER_IDS=your_user_id        # comma-separate to allow more people
   ```
5. Double-click AlphaWave to restart. The scheduler log (`logs/scheduler.log`) shows "Discord approval buttons enabled".

Safety: only the user IDs in `DISCORD_APPROVER_IDS` can click (anyone else gets a private "not allowed"); with no IDs set the feature stays off. Keep the token secret like your Alpaca keys. If the bot can't post, the normal webhook message is sent instead. Limits: Discord can't edit share size or stop/target, so use the dashboard for that.

**Test the buttons before a real trade:** with the scheduler running (double-click AlphaWave first), run `python -m src.discord_approvals --test` from the repo folder (with the venv active). Discord shows a "TEST proposal" with Approve / Reject buttons; click one and the message updates to "Test OK ... Nothing was traded." It never creates a pending order and never touches the broker. If the command reports an error (wrong token, bot not allowed in the channel) it says what to fix.

