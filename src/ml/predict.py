"""Load the saved filter and score a signal — shared by the decision engine and the dashboard."""
from __future__ import annotations

from pathlib import Path

import joblib
import pandas as pd

from src.config import PROJECT_ROOT

MODELS_DIR = PROJECT_ROOT / "models"
_cache: dict[str, dict] = {}


def latest_model_path(models_dir: Path | None = None) -> Path | None:
    d = models_dir or MODELS_DIR
    files = sorted(d.glob("smc_filter_*.pkl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_bundle(path: Path | str | None = None) -> dict | None:
    """Return the model bundle, or None if no model has been trained yet."""
    p = Path(path) if path else latest_model_path()
    if p is None or not p.exists():
        return None
    key = f"{p}:{p.stat().st_mtime}"
    if key not in _cache:
        _cache.clear()
        _cache[key] = joblib.load(p)
    return _cache[key]


def predict_probability(bundle: dict, features: dict[str, float]) -> float:
    """P(win) for one feature dict (built with ml.features.row_features)."""
    X = pd.DataFrame([features], columns=bundle["features"])
    return float(bundle["model"].predict_proba(X)[0, 1])
