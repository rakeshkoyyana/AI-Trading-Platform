# TradingView validation (Phase 2 definition of done: >= 90% match)

This is the one step that needs **you** and TradingView. It takes ~10 minutes per ticker and is fully
automated afterwards.

## 1. Add the export plots to your script
1. Open your combined Pine script in the TradingView Pine editor.
2. Paste the contents of `docs/tradingview_export_patch.pine` at the **very end** of the script
   (after the alert conditions). Don't rename the plot titles.
3. Optional: turn on **Fair Value Gaps** in the indicator settings if you want FVG compared.
4. Save and add to chart.

## 2. Export the chart data
1. Pick a validation window: e.g. **SPY, 15m**, and scroll the chart back so a few thousand bars are loaded
   (the more bars, the better; indicators warm up over ~300 bars).
2. Chart menu (top-right "…" or the layout menu) → **Export chart data…** → leave "Time format: ISO"
   → Export. You get a CSV with `time, open, high, low, close, volume` plus every plot above.
3. Repeat for 2–3 tickers/timeframes (e.g. ASTS 15m, NVDA 15m, SPY 5m).

## 3. Run the validator
```bash
python scripts/validate_against_tradingview.py ~/Downloads/SPY_15m.csv
```
Output: a table per event (TV count, Python count, exact matches, agreement %, ±1-bar agreement),
numeric diffs for EMA/RSI/ATR/pivot levels, and the first few mismatching timestamps.
Exit code 0 means core-event agreement >= 90%.

If your indicator settings differ from the Pine defaults, pass them:
`--swing-length 50 --internal-ob on --swing-ob off --ob-filter atr --ob-mitigation highlow`.

## 4. Reading the results
| Symptom | Likely cause |
|---|---|
| EMA/RSI/ATR diffs tiny (<1e-6) | good — primitives match Pine |
| EMA/RSI/ATR large | export has fewer bars than TV used (warm-up) — export more history |
| Events agree on ±1 bar but not exact | check which timestamp convention the chart uses; report to me |
| Python-only or TV-only events cluster in the first ~300 bars | warm-up; ignore |
| `ob_mit_*` mismatches only | known Pine `for…in` removal quirk (see spec §7) — low impact |

Send me the report output for any ticker below 90% and I'll chase the divergence.

## Strategy trade list (optional)
Strategy Tester → "List of trades" → export CSV. `simulate_strategy()` in
`src/smc_logic/triple_confirmation.py` reproduces entry/exit bars and prices (fills at the next bar's open).
