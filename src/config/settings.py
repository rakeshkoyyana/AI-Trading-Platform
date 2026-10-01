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
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'trading.db'}"

    # --- risk rules --------------------------------------------------------
    max_position_pct: float = 0.05  # max % of equity in one position
    max_daily_loss_pct: float = 0.02  # halt for the day beyond this drawdown
    max_open_positions: int = 3
    risk_per_trade_pct: float = 0.005  # equity risked per trade (stop distance)
    min_model_probability: float = 0.55
    sentiment_block_threshold: float = 0.5  # |score| beyond this blocks counter-trend

    # --- schedule ----------------------------------------------------------
    timezone: str = "America/Chicago"
    session_start: str = "08:30"
    session_end: str = "15:00"

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

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
    return Settings(
        alpaca_api_key=env("ALPACA_API_KEY", ""),
        alpaca_secret_key=env("ALPACA_SECRET_KEY", ""),
        finnhub_api_key=env("FINNHUB_API_KEY", ""),
        newsapi_key=env("NEWSAPI_KEY", ""),
        discord_webhook_url=env("DISCORD_WEBHOOK_URL", ""),
        trading_mode=mode,
        tickers=_csv(env("TICKERS"), DEFAULT_TICKERS),
        timeframe=env("TIMEFRAME", "15Min"),
        database_url=env(
            "DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'trading.db'}"
        ),
        max_position_pct=float(env("MAX_POSITION_PCT", "0.05")),
        max_daily_loss_pct=float(env("MAX_DAILY_LOSS_PCT", "0.02")),
        max_open_positions=int(env("MAX_OPEN_POSITIONS", "3")),
        risk_per_trade_pct=float(env("RISK_PER_TRADE_PCT", "0.005")),
        min_model_probability=float(env("MIN_MODEL_PROBABILITY", "0.55")),
    )
