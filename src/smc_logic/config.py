"""Configuration for the SMC port. Defaults mirror the Pine script's input defaults."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SMCConfig:
    # Real Time Swing / Internal structure
    swing_length: int = 50
    internal_length: int = 5
    internal_confluence_filter: bool = False

    # Order blocks
    internal_order_blocks: bool = True  # Pine default: on
    swing_order_blocks: bool = False  # Pine default: off
    ob_filter: str = "atr"  # "atr" | "range" (cumulative mean range)
    ob_mitigation: str = "highlow"  # "highlow" | "close"
    ob_max_stored: int = 100

    # EQH / EQL
    equal_length: int = 3
    equal_threshold: float = 0.1

    # Fair value gaps
    fvg_auto_threshold: bool = True

    # Triple confirmation
    fast_ema: int = 9
    slow_ema: int = 21
    rsi_length: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    volume_filter: bool = True
    volume_sma: int = 20
    volume_mult: float = 1.2


# Used by the trading pipeline: same logic, but swing OBs on so they can anchor stops.
PIPELINE_CONFIG = SMCConfig(swing_order_blocks=True)
