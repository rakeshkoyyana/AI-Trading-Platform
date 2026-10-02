# SMC + Triple Confirmation — Logic Spec (Pine → Python)

Source: `TripleConfirmation_SMCConcepts.txt` (Pine v5). This document is the contract
the Python port is coded and tested against. Code lives in `src/smc_logic/`.

## 0. Two independent halves

The Pine file contains two scripts concatenated:

1. **Triple Confirmation Strategy** (lines 1–53): EMA trend + RSI momentum + volume spike.
   This is a `strategy()` that actually enters/exits.
2. **Smart Money Concepts (LuxAlgo-style)** (lines 54+): structure, order blocks, EQH/EQL,
   FVG, trailing extremes, premium/discount. This is draw/alert only; it places **no trades**.

They are not wired together in Pine. In the Python platform, the triple-confirmation
rule generates the **signal**, and the SMC outputs become **context features + stop/target
anchors** for the ML filter and decision engine (see §6).

## 1. Triple Confirmation (`triple_confirmation.py`)

| Item | Definition |
|---|---|
| fast EMA / slow EMA | `ta.ema(close, 9)` / `ta.ema(close, 21)` — seeded with SMA of first N bars (Pine behaviour), NaN before |
| trendBullish / trendBearish | `fast > slow` / `fast < slow` |
| RSI | `ta.rsi(close, 14)` — Wilder RMA (SMA-seeded) |
| momentumBullish | `50 < rsi < 70` |
| momentumBearish | `30 < rsi < 50` |
| volumeAvg | `ta.sma(volume, 20)` |
| volume confirmation | `volume > volumeAvg * 1.2` (**same expression for long and short** — volume has no direction in the Pine source; ported as-is) |
| longCondition | trendBullish ∧ momentumBullish ∧ volume spike |
| shortCondition | trendBearish ∧ momentumBearish ∧ volume spike |
| exitLong | trendBearish ∨ `rsi >= 70` |
| exitShort | trendBullish ∨ `rsi <= 30` |

Strategy semantics: orders fill at the **next bar's open** (`process_orders_on_close` off).
`simulate_strategy()` reproduces entry/exit bars and prices for comparison with TradingView's
"List of trades".

**Signal events for the ML pipeline.** `get_signal()` returns the raw level condition
(`long`/`short`/`none` each bar). A *signal event* is the **rising edge** of that series
(first bar where it becomes true). Rationale: live and backfilled signals are then identical
and independent of a hypothetical open position.

## 2. Market structure (`structure.py`) — ported statefully, bar by bar

Per-bar execution order (matches the Pine main block):

1. update trailing extremes
2. update legs/pivots for **swing** (size 50), **internal** (size 5), **equal** (size 3)
3. `displayStructure(internal)` then `displayStructure(swing)` — BOS/CHoCH detection, OB store
4. delete (mitigate) order blocks
5. fair value gaps

**Leg:** `newLegHigh = high[size] > highest(high, size)`; `newLegLow = low[size] < lowest(low, size)`;
leg = 0 (bearish) if newLegHigh, else 1 (bullish) if newLegLow, else unchanged. Initial leg 0.
A change 0→1 confirms a **pivot low** at `low[size]`; 1→0 confirms a **pivot high** at `high[size]`.
Pivots are therefore confirmed `size` bars late (causal, no look-ahead).

**Break:** bullish when `close` crosses over the pivot-high level (`close > L` and `close[1] <= L[1]`)
and the pivot is not already `crossed`. Tag = **CHoCH** if prevailing trend bias was bearish, else **BOS**.
Bearish symmetric with pivot lows. Internal breaks additionally require
`internalLevel != swingLevel` (confluence filter off by default).

**Trend bias:** BULLISH(+1)/BEARISH(−1)/0, kept separately for swing and internal.

## 3. Order blocks (`order_blocks.py`)

- On a structure break, the OB is the extreme **parsed** bar between the broken pivot's bar and the
  current bar: bullish break → bar with the **min parsedLow**; bearish → **max parsedHigh**.
- "Parsed" removes volatile bars: if `(high-low) >= 2 * volatilityMeasure` (ATR(200) by default),
  parsedHigh=low and parsedLow=high.
- Internal OBs on by default; swing OBs off in Pine defaults (configurable here; features enable both).
- Stored newest-first, capped at 100.
- Mitigated (removed) when `high > ob.high` for bearish / `low < ob.low` for bullish
  (`High/Low` mode) or on `close` (`Close` mode).

## 4. EQH / EQL, trailing extremes, zones (`structure.py`, `zones.py`)

- **EQH/EQL:** new equal-length pivot within `0.1 * ATR(200)` of the previous equal pivot.
- **Trailing top/bottom:** `top = max(high, top)`, `bottom = min(low, bottom)` each bar, reset to the
  swing pivot level when a new swing pivot confirms. `NaN` until the first swing pivot.
- **Zones:** premium `[0.95·top+0.05·bottom, top]`, discount `[bottom, 0.95·bottom+0.05·top]`,
  equilibrium `[0.525·bottom+0.475·top, 0.525·top+0.475·bottom]`.

## 5. Fair value gaps (`fvg.py`)

Chart-timeframe FVG (Pine default timeframe `''`):
`barDeltaPercent = (close[1]-open[1]) / (open[1]*100)`, threshold =
`cum(|barDeltaPercent|)/bar_index*2` (auto). Bullish: `low > high[2] ∧ close[1] > high[2] ∧ delta > threshold`.
Bearish symmetric. A gap is removed when price trades through it (`low < bottom` bull / `high > top` bear).

## 6. How SMC feeds the platform

| Output | Used for |
|---|---|
| swing/internal trend bias, bars-since last BOS/CHoCH, last event type | ML features |
| price in/near active order block, distance in ATRs | ML features |
| premium/discount position (0–1) | ML feature + decision gate (don't long deep in premium) |
| FVG presence | ML feature |
| nearest active OB edge / swing pivot | **stop-loss anchor** (beyond the zone) |
| opposing swing/OB level | **take-profit anchor** |

## 7. Known/accepted divergences from TradingView (to check in validation)

1. Pine's `for ... in` while removing array items can skip an element on the bar of a removal;
   Python removes all mitigated OBs each bar. Only affects OB *lifetime* in rare same-bar cases.
2. `na != x` is treated as `True` (Python/NumPy semantics). Only relevant in the first ~50 bars.
3. `ta.cum` of `na` is treated as 0 in the FVG auto-threshold.
4. Exact TradingView bar data differs from Alpaca IEX (volume especially). Validation therefore uses
   **TradingView's own exported bars** (see `docs/TRADINGVIEW_VALIDATION.md`), which isolates logic from data.
