"""Headless-browser check of the interactive chart (skipped when Playwright/Chromium is unavailable)."""
import pandas as pd
import pytest

from src.dashboard import data as D
from src.dashboard.chart_component import chart_html
from src.data_ingestion.synthetic import make_bars
from src.smc_logic import compute_context

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    bars = make_bars(n_days=40, seed=5)
    ctx = compute_context(bars)
    tr = pd.DataFrame([dict(symbol="AAA", entry_time=bars["timestamp"].iat[900], exit_time=bars["timestamp"].iat[912], direction="long",
                            entry_price=float(bars["close"].iat[900]), exit_price=float(bars["close"].iat[912]), qty=10, pnl=12.0,
                            stop_loss=float(bars["close"].iat[900]) * 0.99, take_profit=float(bars["close"].iat[900]) * 1.02)])
    f = tmp_path_factory.mktemp("chart") / "chart.html"
    f.write_text(chart_html(D.build_chart_payload(bars, ctx, tr, "AAA")))
    with sync_api.sync_playwright() as p:
        try:
            br = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available: {exc}")
        pg = br.new_page(viewport={"width": 1400, "height": 760})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(f"file://{f}")
        pg.wait_for_timeout(1200)
        yield pg
        br.close()


def test_renders_without_js_errors_and_shows_legend(page):
    assert page.errors == []
    assert "AAA" in page.inner_text("#legend") and "RSI" in page.inner_text("#legend")
    assert page.locator("#chart canvas").count() >= 3  # price + volume + RSI panes


def test_timeframe_switch_and_toggles(page):
    page.click("button[data-tf='1D']")
    page.wait_for_timeout(300)
    assert "on" in page.get_attribute("button[data-tf='1D']", "class")
    page.click("button[data-tf='15m']")
    before = page.get_attribute("button[data-k='pd']", "class")
    page.click("button[data-k='pd']")
    assert page.get_attribute("button[data-k='pd']", "class") != before
    assert page.errors == []


def test_measure_tool_reads_out_move(page):
    page.click("button[data-t=measure]")
    page.mouse.move(500, 350)
    page.mouse.click(500, 350)
    page.wait_for_timeout(500)
    page.mouse.move(900, 250)
    page.wait_for_timeout(200)
    page.mouse.click(900, 250)
    page.wait_for_timeout(400)
    txt = page.inner_text("#measure")
    assert "bars" in txt and "%" in txt
    page.keyboard.press("Escape")
    assert page.errors == []
