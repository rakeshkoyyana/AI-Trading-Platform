"""Generate the AlphaWave brand files (SVG + PNG) from code, so they can be tweaked and rebuilt.

    python assets/brand/build_brand.py

The wordmark is converted to outlines (Inter, SIL Open Font License) so it renders identically everywhere.
Needs fontTools and, for PNG export, Playwright with Chromium.
"""
from __future__ import annotations

from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).parent
FONTS = Path("/usr/share/fonts/opentype/inter")
A, B = "#6c8cff", "#2ee6c0"  # indigo -> mint

# The alpha: one continuous stroke that loops once, so it reads as a letter and as a wave.
ALPHA = "M40 15 C31 15 24 33 15.5 33 C9.5 33 9.5 15 15.5 15 C24 15 31 33 40 33"


def text_path(txt: str, font_file: str, size: float, x: float, y: float, tracking: float = 0.0) -> tuple[str, float]:
    f = TTFont(FONTS / font_file)
    gs, cmap, upm = f.getGlyphSet(), f.getBestCmap(), f["head"].unitsPerEm
    sc, d = size / upm, []
    for ch in txt:
        g = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (sc, 0, 0, -sc, x, y)))
        d.append(pen.getCommands())
        x += gs[g].width * sc + tracking
    return " ".join(d), x


def grad(i: str, x1=0, y1=0, x2=1, y2=1) -> str:
    return (f'<linearGradient id="{i}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{A}"/><stop offset="1" stop-color="{B}"/></linearGradient>')


def mark_tile() -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="512" height="512"><defs>{grad("g")}</defs>'
            f'<rect width="48" height="48" rx="11" fill="url(#g)"/>'
            f'<path d="{ALPHA}" fill="none" stroke="#fff" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def lockup(on_dark: bool) -> str:
    word_a, x = text_path("Alpha", "Inter-Light.otf", 30, 58, 33.5, tracking=-0.3)
    word_w, x2 = text_path("Wave", "Inter-Bold.otf", 30, x + 0.5, 33.5, tracking=-0.3)
    ink = "#e9ecfa" if on_dark else "#12141f"
    w = x2 + 4
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} 48" width="{w * 4:.0f}" height="192"><defs>{grad("g")}'
            f'<linearGradient id="t" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{A}"/><stop offset="1" stop-color="{B}"/></linearGradient></defs>'
            f'<rect x="0" y="0" width="48" height="48" rx="11" fill="url(#g)"/>'
            f'<path d="{ALPHA}" fill="none" stroke="#fff" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<path d="{word_a}" fill="{ink}"/><path d="{word_w}" fill="url(#t)"/></svg>')


def main() -> None:
    (OUT / "alphawave-mark.svg").write_text(mark_tile())
    (OUT / "alphawave-lockup-dark.svg").write_text(lockup(True))    # for dark backgrounds
    (OUT / "alphawave-lockup-light.svg").write_text(lockup(False))  # for light backgrounds
    sheet = (f'<html><body style="margin:0;display:flex;flex-direction:column;font-family:Inter,sans-serif">'
             f'<div style="background:#12141f;padding:48px 56px;display:flex;align-items:center;gap:48px;width:1300px;box-sizing:border-box">'
             f'<div style="flex:none">{lockup(True).replace(chr(34)+"192"+chr(34), chr(34)+"130"+chr(34))}</div>'
             f'<div style="flex:none;width:120px">{mark_tile().replace(chr(34)+"512"+chr(34), chr(34)+"120"+chr(34))}</div>'
             f'<div style="flex:none;width:48px">{mark_tile().replace(chr(34)+"512"+chr(34), chr(34)+"48"+chr(34))}</div>'
             f'<div style="flex:none;width:24px">{mark_tile().replace(chr(34)+"512"+chr(34), chr(34)+"24"+chr(34))}</div></div>'
             f'<div style="background:#f6f7fb;padding:48px 56px;width:1300px;box-sizing:border-box"><div style="flex:none">{lockup(False).replace(chr(34)+"192"+chr(34), chr(34)+"130"+chr(34))}</div></div>'
             f'</body></html>')
    (OUT / "_sheet.html").write_text(sheet)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1300, "height": 420}, device_scale_factor=2)
            pg.goto(f"file://{OUT / '_sheet.html'}")
            pg.screenshot(path=str(OUT / "alphawave-preview.png"), full_page=True)
            for name, size in (("alphawave-mark-512.png", 512), ("favicon.png", 128)):
                pg = br.new_page(viewport={"width": size, "height": size})
                pg.set_content(f'<body style="margin:0;background:transparent">{mark_tile().replace(chr(34)+"512"+chr(34), chr(34)+str(size)+chr(34))}</body>')
                pg.screenshot(path=str(OUT / name), omit_background=True)
            pg = br.new_page(viewport={"width": 1040, "height": 200}, device_scale_factor=1)
            pg.set_content(f'<body style="margin:0;background:transparent">{lockup(True)}</body>')
            pg.locator("svg").screenshot(path=str(OUT / "alphawave-lockup-dark.png"), omit_background=True)
            pg = br.new_page(viewport={"width": 1040, "height": 200}, device_scale_factor=1)
            pg.set_content(f'<body style="margin:0;background:transparent">{lockup(False)}</body>')
            pg.locator("svg").screenshot(path=str(OUT / "alphawave-lockup-light.png"), omit_background=True)
            br.close()
    except Exception as exc:  # noqa: BLE001
        print("PNG export skipped:", exc)
    (OUT / "_sheet.html").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
