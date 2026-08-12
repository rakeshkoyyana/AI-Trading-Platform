# Product Requirements Document (PRD) — AI-Powered SMC Trading Agent

**Status:** Draft — source of truth for scope, superseded only by explicit team agreement
**Owners:** Rakesh Koyyana + 1 collaborator
**Repo:** https://github.com/rakeshkoyyana/AI-Trading-Platform

---

## 1. Summary

Build an automated trading agent that ports an existing Pine Script "triple confirmation" Smart Money Concepts (SMC) strategy into Python, layers a machine-learning win-probability filter and live news sentiment scoring on top of it, executes trades on a schedule during market hours, and reports performance on a live dashboard. The system trades on paper (simulated money) through most of its life; going live with real money is an explicit, separately-approved final phase.

This is a two-person, part-time, engineering-education project — not a funded trading desk. Scope and tooling choices throughout this PRD are made accordingly: free-tier services, sequential (not parallel) task ownership, and a strong bias toward "working and verifiable" over "theoretically optimal."

## 2. Problem / motivation

An existing manually-run TradingView Pine Script strategy (SMC triple confirmation) has no automation, no systematic backtest, no ML-based filtering of low-quality setups, and no sentiment awareness. This project turns that manual strategy into a tested, monitored, automated system.

## 3. Goals

- Faithfully port the Pine Script SMC logic to Python, validated against TradingView output.
- Add a machine-learning filter that improves win rate / risk-adjusted return versus the raw unfiltered signal.
- Incorporate live news sentiment as a gating signal.
- Execute automatically, on schedule, against a paper trading account, with hard risk limits.
- Provide a dashboard with enough visibility to debug *why* any given trade happened.
- Do all of the above with credentials and secrets managed safely and shareable between two people without pasting them in chat.

## 4. Non-goals

- No guarantee of profitability. This is an engineering project, not investment advice.
- No live/real-money trading until Phase 10, and only after a statistically meaningful paper track record.
- No support for brokers beyond Alpaca in the initial build (Robinhood integration is optional, isolated, and explicitly opt-in due to ToS risk).
- No mobile app — dashboard is a Streamlit web app only.
- No enterprise-grade secrets infrastructure (Vault, dynamic secrets) — a hosted secrets manager (Doppler) is sufficient at this scale.

## 5. Users

- **Primary users:** the two developers (Rakesh + collaborator), who are also the sole operators/traders.
- **Secondary "user"** of the dashboard: the same two people, checking performance daily/weekly.

## 6. System overview

```
Market Data (Alpaca/yfinance) ──┐
                                  ├──> Feature Engineering (SMC logic ported from Pine Script) ──> ML Model (win-probability filter) ──┐
News Feed (Finnhub/NewsAPI) ────┤                                                                                                     │
                                  └──> Sentiment Scoring (FinBERT, local)  ──────────────────────────────────────────────────────────┤
                                                                                                                                        ↓
                                                                                                                          Decision Engine (triple confirmation + ML + sentiment + risk rules)
                                                                                                                                        ↓
                                                                                                                          Execution Layer (Alpaca first, Robinhood optional)
                                                                                                                                        ↓
                                                                                                                          Trade Logger (SQLite / Supabase)
                                                                                                                                        ↓
                                                                                                          Scheduler (8:30am–3:00pm CST) ⇄ Dashboard (Streamlit)

Cross-cutting: Secrets Manager (Doppler) supplies every credential above at runtime.
```

## 7. Functional requirements, by phase

Each phase below has explicit **acceptance criteria** (the PRD-level "definition of done"). Full task breakdowns live in `PLAN.md`.

### Phase 0 — Environment & Secrets (COMPLETE, retroactively re-scoped to include secrets management)
- Python 3.11 venv, repo structure, `.gitignore`, `requirements.txt` installed.
- Alpaca (paper), Finnhub, NewsAPI, Discord webhook credentials created.
- **New requirement:** credentials are stored in a shared secrets manager (Doppler) and retrieved via API/CLI at runtime, not distributed as a copy-pasted local `.env` file between teammates.
- `smoke_test.py` connects to Alpaca and prints paper account equity, sourcing its credentials from Doppler.
- **Acceptance criteria:** both teammates can run `doppler run -- python src/smoke_test.py` on their own machines and get a successful connection, without ever receiving a raw secret over chat/email.

### Phase 1 — Data Pipeline
- Pull historical + live OHLCV bars from Alpaca, with yfinance as a fallback, into a normalized schema.
- **Acceptance criteria:** `backfill.py` populates months of historical bars for all target tickers; ingestion tests pass; no NaNs in OHLC.

### Phase 2 — SMC Logic Port
- Every Pine Script condition (order blocks, FVG, BOS, CHoCH, premium/discount zones, triple-confirmation) ported to Python and validated against TradingView.
- **Acceptance criteria:** ≥90% signal match (timestamp + direction) against TradingView on 3–5 validation windows; `signals` table fully backfilled and labeled.

### Phase 3 — News & Sentiment
- News fetched (Finnhub primary, NewsAPI fallback), cached, scored with local FinBERT, aggregated into a rolling per-symbol sentiment score.
- **Acceptance criteria:** rolling sentiment updates at least hourly during market hours without exceeding free-tier API quotas.

### Phase 4 — ML Model
- Win-probability classifier trained on signal + sentiment + technical features, using walk-forward validation (no random shuffling).
- **Acceptance criteria:** filtered signal set outperforms unfiltered SMC signals on held-out data; model + metadata saved and versioned.

### Phase 5 — Decision Engine & Risk
- Combines SMC signal + ML probability + sentiment gate + hard risk rules (max position size, daily loss halt, max concurrent positions, stop/take-profit).
- **Acceptance criteria:** decisions are explainable/traceable on sample historical bars; paper/live mode flag wired through to execution.

### Phase 6 — Execution Integration
- Broker-agnostic interface, Alpaca adapter (primary), optional isolated Robinhood adapter.
- **Acceptance criteria:** manual test order shows up correctly in Alpaca paper account and is logged.

### Phase 7 — Scheduler
- Market-hours-aware (8:30am–3:00pm CST) automated run loop with error handling and Discord alerting.
- **Acceptance criteria:** runs unattended for at least one full real trading day without crashing.

### Phase 8 — Dashboard
- Streamlit dashboard: KPIs, equity curve, trade P&L distribution, win rate by signal type/sentiment, price chart with overlays, trade log, system health panel.
- **Acceptance criteria:** deployed to Streamlit Community Cloud, reflects latest paper trades, secrets sourced from Doppler (not a committed `.env`).

### Phase 9 — Paper Trading & Iteration
- Minimum 2–4 weeks continuous paper trading; weekly comparison against backtest expectations; periodic model retraining.
- **Acceptance criteria:** statistically meaningful trade count with no unresolved scheduler errors.

### Phase 10 — Go-Live (optional, gated)
- Small live position sizing, confirmed kill-switch, close monitoring.
- **Acceptance criteria:** explicit go/no-go decision made jointly by both teammates after reviewing the paper track record; separate `prod` secrets config used, isolated from `dev`.

## 8. Non-functional requirements

- **Security:** no secret ever committed to git or pasted in shared chat; `.env` (if used at all locally) stays gitignored; production secrets isolated from dev secrets in Doppler.
- **Cost:** every tool must have a workable free tier; costs called out explicitly if a free tier is exceeded.
- **Reliability:** the scheduler must not crash the whole process on a single bad cycle — errors are caught, logged, and alerted via Discord.
- **Auditability:** every trade attempt, fill, and rejection is logged to the `trades` table immediately.
- **Collaboration:** both teammates can independently set up their local environment (including secrets) without direct file transfer between machines.

## 9. Risks

| Risk | Mitigation |
|---|---|
| SMC logic port doesn't match Pine Script | Budget generously for Phase 2; validate against TradingView on multiple windows before proceeding |
| Free-tier API limits hit mid-build | Caching built in from Phase 3 onward; NewsAPI as Finnhub fallback |
| Secrets leaked via chat/email (already happened once during setup) | Doppler migration (see `SECRETS_MANAGEMENT_PLAN.md`); rotate any secret that was ever pasted in plaintext |
| Model overfits / looks good only in-sample | Walk-forward validation only, never random shuffling |
| Robinhood ToS violation | Robinhood adapter isolated, optional, off by default |
| Unattended scheduler failure during market hours | Try/except wrapping + Discord alerting on every cycle |

## 10. Success metrics

- Phase 2 signal match rate ≥90% vs. TradingView.
- ML-filtered signals show measurably better win rate / risk-adjusted return than unfiltered signals (Phase 4).
- Scheduler runs unattended for a full trading day with zero unhandled crashes (Phase 7).
- At least 2–4 weeks of continuous paper trading data with performance in line with backtest expectations (Phase 9).
- Zero secrets committed to git or shared as plaintext for the remainder of the project (ongoing, starting now).

## 11. Open questions

- Which specific tickers are in scope for Phase 1 backfill?
- What holding horizon defines "win/loss" for signal labeling in Phase 2?
- Who owns which phase first — sequential handoff order still needs to be assigned between the two teammates.
- Doppler vs. staying on `.env` for solo local dev, with Doppler only for shared/deployed secrets — decide during Phase 0 secrets migration.

## 12. References

- `docs/PROJECT_PLAN.md` — original detailed engineering plan (source material for this PRD)
- `TASKS.md` — flat checklist, now superseded by `PLAN.md` as the actively-maintained task tracker
- `SECRETS_MANAGEMENT_PLAN.md` — detailed secrets migration plan referenced in Phase 0 above
