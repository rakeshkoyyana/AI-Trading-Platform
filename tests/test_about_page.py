"""The About AlphaWave page: it loads without errors, every chapter renders, and the sizing calculator matches the engine's maths."""
import pytest

from src.dashboard import chart_component, theme

sync_api = pytest.importorskip("playwright.sync_api")
URL = "file://" + str(chart_component.STATIC / "about.html")


def test_helper_and_brand_link():
    assert "AlphaWave" in chart_component.about_html()
    assert 'href="#about-alphawave"' in theme.topbar(False, True, None, False, 1000.0, "now")


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium not available: {exc}")
        pg = br.new_page(viewport={"width": 1280, "height": 900})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(URL)
        pg.wait_for_timeout(500)
        yield pg
        br.close()


def test_every_chapter_renders_without_errors(page):
    for ch in ["story", "idea", "machine", "life", "guard", "road"]:
        page.click(f'button[data-ch="{ch}"]')
        page.wait_for_timeout(200)
        assert page.is_visible(f"#ch-{ch}")
    assert page.errors == []


def test_architecture_signal_animation_runs(page):
    page.click('button[data-ch="machine"]')
    page.click("#fire")
    page.wait_for_timeout(1200)
    assert "Step" in page.inner_text("#caption")
    page.wait_for_function("!document.querySelector('#caption').innerText.includes('Step 1 of')", timeout=6000)


def test_calculator_asts_preset_is_capped_at_86_shares(page):
    page.click('button[data-ch="life"]')
    page.click("#preset")
    assert "86" in page.inner_text("#ckpis")
