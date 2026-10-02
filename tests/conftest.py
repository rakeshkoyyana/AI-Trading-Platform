import sys
from pathlib import Path

# Make `import src...` work when pytest is run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_live_tail(monkeypatch):
    """Tests never call the real-time IEX feed; the live tail has its own unit tests with injected data."""
    monkeypatch.setenv("LIVE_HYBRID", "false")
