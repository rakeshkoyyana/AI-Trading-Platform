"""Render the interactive TradingView-style chart (Lightweight Charts, vendored for offline use)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"


@lru_cache(maxsize=1)
def _template() -> tuple[str, str]:
    return (STATIC / "chart.html").read_text(), (STATIC / "lightweight-charts.standalone.production.js").read_text()


def chart_html(payload: dict) -> str:
    """Self-contained HTML (library + data inlined) for st.components.v1.html or a standalone page."""
    html, lib = _template()
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")  # keep </script> out of the JSON
    return html.replace("/*__LIB__*/", lib.replace("</script", "<\\/script")).replace("__PAYLOAD__", data)
