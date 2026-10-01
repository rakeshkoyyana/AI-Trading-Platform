#!/usr/bin/env python3
"""Validate the Python SMC port against a TradingView chart-data export.

    python scripts/validate_against_tradingview.py ~/Downloads/SPY_15m_export.csv
    python scripts/validate_against_tradingview.py export.csv --swing-length 50 --internal-ob on --swing-ob off
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.smc_logic.config import SMCConfig  # noqa: E402
from src.smc_logic.tv_validation import format_report, validate  # noqa: E402


def _onoff(v: str) -> bool:
    return v.lower() in {"on", "true", "1", "yes"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv")
    p.add_argument("--swing-length", type=int, default=50)
    p.add_argument("--internal-ob", default="on")
    p.add_argument("--swing-ob", default="off")
    p.add_argument("--ob-filter", default="atr", choices=["atr", "range"])
    p.add_argument("--ob-mitigation", default="highlow", choices=["highlow", "close"])
    p.add_argument("--no-volume-filter", action="store_true")
    a = p.parse_args()
    cfg = SMCConfig(
        swing_length=a.swing_length,
        internal_order_blocks=_onoff(a.internal_ob),
        swing_order_blocks=_onoff(a.swing_ob),
        ob_filter=a.ob_filter,
        ob_mitigation=a.ob_mitigation,
        volume_filter=not a.no_volume_filter,
    )
    res = validate(a.csv, cfg)
    print(format_report(res))
    ov = res["overall_agreement"]
    return 0 if ov == ov and ov >= 0.90 else 1


if __name__ == "__main__":
    sys.exit(main())
