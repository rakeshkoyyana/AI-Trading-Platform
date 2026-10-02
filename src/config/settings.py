"""
Central configuration.

Secrets come from environment variables. That works for a local `.env`
(loaded via python-dotenv), for `doppler run -- <cmd>` (Phase 0.5), and for
Streamlit Cloud secrets exported as env vars. Nothing here ever hardcodes a key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Default watchlist. Override with TICKERS="ASTS,SPY,..." in the environment.
DEFAULT_TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "ASTS"]


def _csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [v.strip().upper() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # --- secrets -----------------------------------------------------------
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    finnhub_api_key: str = ""
    newsapi_key: str = ""
    discord_webhook_url: str = ""

    # --- trading mode / safety --------------------------------------------
    trading_mode: str = "paper"  # "paper" | "live"
    kill_switch_file: Path = PROJECT_ROOT / "KILL_SWITCH"

    # --- data --------------------------------------------------------------
    tickers: list[str] = field(default_factory=lambda: list(DEFAULT_TICKERS))
    timeframe: str = "15Min"  # "5Min" | "15Min" | "1Hour" | "1Day"
    # "sip" = consolidated tape of all US exchanges (what TradingView shows: same prices, same volume, full
    # extended hours). "iex" = one exchange only (real-time on the free plan, but ~2% of the volume).
    alpaca_data_feed: str = "sip"
    # Alpaca's free plan serves SIP only for data older than 15 minutes. Everything that reads bars therefore
    # works on data at least this many minutes old. Set 0 if the account has a real-time SIP subscription.
    sip_delay_minutes: int = 16
    # True: indicators/signals/models use every stored bar (pre/post-market included, matching a
    # TradingView chart with "Extended hours" on). False: regular session 09:30-16:00 ET only.
    # Bars are always STORED unfiltered; this only changes what load_bars() returns.
    include_extended_hours: bool = True
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'trading.db'}"

    # --- risk rules --------------------------------------------------------
    max_position_pct: float = 0.05  # max % of equity in one position
    max_daily_loss_pct: float = 0.02  # halt for the day beyond this drawdown
    max_open_positions: int = 3
    risk_per_trade_pct: float = 0.005  # equity risked per trade (stop distance)
    min_model_probability: float = 0.55
    sentiment_block_threshold: float = 0.5  # |score| beyond this blocks counter-trend
    allow_shorts: bool = True
    use_unvalidated_model: bool = False  # True: let a model that did NOT beat raw signals OOS gate trades
    require_model: bool = False  # True: refuse to trade until a trained model exists
    max_stop_pct: float = 0.05  # reject setups whose stop is wider than this % of price
    min_stop_atr: float = 0.25  # reject stops tighter than this many ATRs (noise)
    min_rr: float = 1.5
    signal_max_age_bars: int = 2  # ignore signals older than this many bars (stale data guard)
    flatten_at_close: bool = True  # close all positions at session end (day-trading)
    flatten_minutes_before_close: int = 5  # orders sent AT the close can't fill until tomorrow
    no_new_entries_minutes_before_close: int = 15
    bar_delay_seconds: int = 20  # wait after a bar closes before fetching it

    # --- schedule ----------------------------------------------------------
    timezone: str = "America/Chicago"
    session_start: str = "08:30"
    session_end: str = "15:00"

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def data_delay_minutes(self) -> int:
        """How far behind the wall clock the usable market data is."""
        return max(self.sip_delay_minutes, 0) if self.alpaca_data_feed == "sip" else 0

    @property
    def kill_switch_active(self) -> bool:
        return self.kill_switch_file.exists()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    env = os.getenv
    mode = (env("TRADING_MODE", "paper") or "paper").lower()
    if mode not in {"paper", "live"}:
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")
    if (env("ALPACA_DATA_FEED", "sip") or "sip").lower() not in {"sip", "iex"}:
        raise ValueError("ALPACA_DATA_FEED must be 'sip' or 'iex'")
    return Settings(
        alpaca_api_key=env("ALPACA_API_KEY", ""),
        alpaca_secret_key=env("ALPACA_SECRET_KEY", ""),
        finnhub_api_key=env("FINNHUB_API_KEY", ""),
        newsapi_key=env("NEWSAPI_KEY", ""),
        discord_webhook_url=env("DISCORD_WEBHOOK_URL", ""),
        trading_mode=mode,
        tickers=_csv(env("TICKERS"), DEFAULT_TICKERS),
        timeframe=env("TIMEFRAME", "15Min"),
        alpaca_data_feed=(env("ALPACA_DATA_FEED", "sip") or "sip").lower(),
        sip_delay_minutes=int(env("SIP_DELAY_MINUTES", "16")),
        include_extended_hours=env("INCLUDE_EXTENDED_HOURS", "true").lower() in {"1", "true", "yes", "on"},
        database_url=env(
            "DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'trading.db'}"
        ),
        max_position_pct=float(env("MAX_POSITION_PCT", "0.05")),
        max_daily_loss_pct=float(env("MAX_DAILY_LOSS_PCT", "0.02")),
        max_open_positions=int(env("MAX_OPEN_POSITIONS", "3")),
        risk_per_trade_pct=float(env("RISK_PER_TRADE_PCT", "0.005")),
        min_model_probability=float(env("MIN_MODEL_PROBABILITY", "0.55")),
        sentiment_block_threshold=float(env("SENTIMENT_BLOCK_THRESHOLD", "0.5")),
        allow_shorts=env("ALLOW_SHORTS", "true").lower() in {"1", "true", "yes", "on"},
        use_unvalidated_model=env("USE_UNVALIDATED_MODEL", "false").lower() in {"1", "true", "yes", "on"},
        require_model=env("REQUIRE_MODEL", "false").lower() in {"1", "true", "yes", "on"},
        max_stop_pct=float(env("MAX_STOP_PCT", "0.05")),
        min_stop_atr=float(env("MIN_STOP_ATR", "0.25")),
        min_rr=float(env("MIN_RR", "1.5")),
        signal_max_age_bars=int(env("SIGNAL_MAX_AGE_BARS", "2")),
        flatten_at_close=env("FLATTEN_AT_CLOSE", "true").lower() in {"1", "true", "yes", "on"},
        flatten_minutes_before_close=int(env("FLATTEN_MINUTES_BEFORE_CLOSE", "5")),
        no_new_entries_minutes_before_close=int(env("NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE", "15")),
        bar_delay_seconds=int(env("BAR_DELAY_SECONDS", "20")),
    )
