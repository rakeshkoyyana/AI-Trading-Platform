"""AlphaWave brand assets for the dashboard (files live in assets/brand/, built by build_brand.py)."""
from __future__ import annotations

import base64
import re
from pathlib import Path

from src.config import PROJECT_ROOT

NAME = "AlphaWave"
BRAND_DIR = PROJECT_ROOT / "assets" / "brand"
FAVICON = BRAND_DIR / "favicon.png"


def lockup_svg(height: int = 30) -> str:
    """Inline logo (dark-background version) at the given pixel height; empty string if the file is missing."""
    try:
        svg = (BRAND_DIR / "alphawave-lockup-dark.svg").read_text()
    except OSError:
        return f"<b>{NAME}</b>"
    vb = re.search(r'viewBox="0 0 (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)"', svg)
    width = round(height * float(vb.group(1)) / float(vb.group(2))) if vb else height * 5
    svg = re.sub(r'\swidth="\d+"\sheight="\d+"', f' width="{width}" height="{height}" style="display:block"', svg, count=1)
    return svg.replace('id="g"', 'id="awg"').replace('url(#g)', 'url(#awg)').replace('id="t"', 'id="awt"').replace('url(#t)', 'url(#awt)')


def lockup_img(height: int = 28) -> str:
    """The logo as a data-URI <img>: survives HTML sanitisers that strip inline <svg> gradients."""
    svg = lockup_svg(height)
    if not svg.startswith("<svg"):
        return svg
    b64 = base64.b64encode(svg.encode()).decode()
    return f'<img alt="{NAME}" height="{height}" style="display:block" src="data:image/svg+xml;base64,{b64}">'


def page_icon() -> str:
    return str(FAVICON) if FAVICON.exists() else "📈"
