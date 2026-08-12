"""
Phase 0 smoke test.

Loads API credentials from .env and pings Alpaca's paper trading
account endpoint to confirm the environment is wired up correctly.

Run with:
    python src/smoke_test.py
"""
import os
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print(
            "ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.\n"
            "Check your .env file."
        )
        return 1

    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: pip install -r requirements.txt")
        return 1

    try:
        client = TradingClient(api_key, secret_key, paper=True)
        account = client.get_account()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not reach Alpaca API: {exc}")
        return 1

    print("Alpaca connection OK.")
    print(f"  Account status : {account.status}")
    print(f"  Paper equity   : ${account.equity}")
    print(f"  Buying power   : ${account.buying_power}")
    return 0


if __name__ == "__main__":
    sys.exit(main())