"""The trade list is yours to edit: seeded from .env once, then add / remove; the scheduler follows it."""
from datetime import datetime

import pytest

from src.config.settings import Settings
from src.db.schema import Trade, get_engine, init_db, session_scope
from src.execution import control

S = Settings(tickers=["AAA", "BBB"], default_trade_mode="ask")


@pytest.fixture()
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path/'d.db'}")
    init_db(e)
    return e


def test_list_is_seeded_from_env_then_owned_by_the_user(engine):
    assert control.trade_tickers(engine, S) == ["AAA", "BBB"]
    assert control.remove_trade_ticker(engine, "AAA", S) == (True, "")
    assert control.trade_tickers(engine, S) == ["BBB"]            # a removed .env ticker does NOT come back
    assert control.trade_tickers(engine, S) == ["BBB"]


def test_removed_ticker_is_no_longer_scanned_by_the_scheduler(engine):
    assert set(control.active_symbols(engine, S)) == {"AAA", "BBB"}
    control.remove_trade_ticker(engine, "AAA", S)
    assert set(control.active_symbols(engine, S)) == {"BBB"}


def test_added_ticker_starts_off_and_is_not_scanned_until_you_pick_a_mode(engine):
    ok, _ = control.add_trade_ticker(engine, "nvda", S)
    assert ok
    assert "NVDA" in control.trade_tickers(engine, S)
    assert control.get_modes(engine, settings=S)["NVDA"] == "off"
    assert "NVDA" not in control.active_symbols(engine, S)
    control.set_mode(engine, "NVDA", "ask")
    assert control.active_symbols(engine, S)["NVDA"] == "ask"
    assert control.add_trade_ticker(engine, "NVDA", S)[0] is False  # duplicate
    assert control.add_trade_ticker(engine, "$$$", S)[0] is False   # junk


def test_cannot_remove_a_ticker_with_an_open_position(engine):
    with session_scope(engine) as sx:
        sx.add(Trade(symbol="AAA", direction="long", entry_time=datetime.utcnow(), entry_price=10, qty=1, status="filled"))
    ok, why = control.remove_trade_ticker(engine, "AAA", S)
    assert not ok and "open position" in why
    assert "AAA" in control.trade_tickers(engine, S)


def test_tickers_you_had_already_switched_on_survive_the_first_seed(engine):
    control.set_mode(engine, "ZZZ", "auto")      # e.g. a searched ticker you had set to Auto before this feature existed
    assert control.trade_tickers(engine, S) == ["AAA", "BBB", "ZZZ"]
    assert control.active_symbols(engine, S)["ZZZ"] == "auto"
