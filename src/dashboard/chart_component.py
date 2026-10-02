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


BRIDGE = Path(__file__).resolve().parent / "chart_bridge"


def chart_widget(html: str, key: str, height: int = 760):
    """Show the chart page and return what the user did on it (confirm / reject / apply), or None.

    A tiny Streamlit component v1 wraps the page so the lines dragged on the chart can talk back to Python.
    The value is sticky across reruns, so callers de-duplicate on its `seq` field.
    """
    import streamlit.components.v1 as components

    global _component
    if _component is None:
        _component = components.declare_component("alphawave_chart", path=str(BRIDGE))
    return _component(html=html, height=height, key=key, default=None)


_component = None
