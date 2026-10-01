"""
Walk-forward training and evaluation of the signal-quality filter.

    python -m src.ml.train --timeframe 15Min --model xgb --threshold 0.55 --version v1

Evaluation is strictly out-of-sample and time-ordered: expanding training window, test on the
next time chunk, with an embargo gap so a training label's forward window can never overlap the
test period. Random shuffling is never used.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.config import PROJECT_ROOT, get_settings
from src.ml.features import FEATURES, build_feature_matrix

MODELS_DIR = PROJECT_ROOT / "models"


def make_model(kind: str = "xgb"):
    if kind == "logreg":
        return make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=0.3, max_iter=1000)
        )
    from xgboost import XGBClassifier

    return XGBClassifier(
        n_estimators=150, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
        min_child_weight=5, reg_lambda=2.0, eval_metric="logloss", random_state=42, n_jobs=1,
    )


@dataclass
class Fold:
    k: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def walk_forward_splits(
    timestamps: pd.Series,
    n_splits: int = 4,
    embargo_days: float = 1.0,
    min_train: int = 50,
    min_test: int = 15,
) -> list[Fold]:
    """Expanding-window folds over time. Fold k trains on chunks < k (minus embargo), tests on chunk k."""
    ts = pd.to_datetime(timestamps).reset_index(drop=True)
    lo, hi = ts.min(), ts.max()
    edges = pd.date_range(lo, hi, periods=n_splits + 2)  # n_splits+1 chunks
    folds = []
    for k in range(1, n_splits + 1):
        t0, t1 = edges[k], edges[k + 1]
        last = k == n_splits
        test = np.where((ts >= t0) & ((ts <= t1) if last else (ts < t1)))[0]
        train = np.where(ts < t0 - pd.Timedelta(days=embargo_days))[0]
        if len(train) >= min_train and len(test) >= min_test:
            folds.append(Fold(k, train, test, t0, t1))
    return folds


def summarize(oof: pd.DataFrame, threshold: float) -> dict:
    """Metrics on out-of-sample predictions. oof needs: y, p, forward_return."""
    y, p, r = oof["y"].to_numpy(int), oof["p"].to_numpy(float), oof["forward_return"].to_numpy(float)
    taken = p >= threshold

    def stats(mask):
        n = int(mask.sum())
        if n == 0:
            return dict(n=0, win_rate=np.nan, avg_return=np.nan, total_return=0.0, profit_factor=np.nan, sharpe=np.nan)
        rr = r[mask]
        gains, losses = rr[rr > 0].sum(), -rr[rr < 0].sum()
        sd = rr.std(ddof=1) if n > 1 else np.nan
        return dict(
            n=n, win_rate=float(y[mask].mean()), avg_return=float(rr.mean()), total_return=float(rr.sum()),
            profit_factor=float(gains / losses) if losses > 0 else float("inf"),
            sharpe=float(rr.mean() / sd * np.sqrt(n)) if sd and sd > 0 else np.nan,
        )

    out = dict(
        n_oos=len(y),
        auc=float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else float("nan"),
        brier=float(brier_score_loss(y, p)),
        base_rate=float(y.mean()),
        threshold=threshold,
        unfiltered=stats(np.ones(len(y), bool)),
        filtered=stats(taken),
    )
    tn = (~taken)
    out["precision"] = float(y[taken].mean()) if taken.any() else float("nan")
    out["recall"] = float(taken[y == 1].mean()) if (y == 1).any() else float("nan")
    out["skipped"] = stats(tn)
    wr_u, wr_f = out["unfiltered"]["win_rate"], out["filtered"]["win_rate"]
    out["improves"] = bool(
        out["filtered"]["n"] >= 30 and wr_f == wr_f and wr_f > wr_u + 0.02
        and out["filtered"]["avg_return"] > out["unfiltered"]["avg_return"]
    )
    return out


def calibration_table(oof: pd.DataFrame, bins: int = 5) -> pd.DataFrame:
    q = pd.qcut(oof["p"], q=min(bins, oof["p"].nunique()), duplicates="drop")
    g = oof.groupby(q, observed=True).agg(n=("y", "size"), mean_pred=("p", "mean"), actual_win_rate=("y", "mean"))
    return g.reset_index(drop=True)


def evaluate_walk_forward(
    X: pd.DataFrame, y: pd.Series, meta: pd.DataFrame, kind: str = "xgb", threshold: float = 0.55,
    n_splits: int = 4, embargo_days: float = 1.0,
) -> tuple[dict, pd.DataFrame, list[Fold]]:
    folds = walk_forward_splits(meta["timestamp"], n_splits, embargo_days)
    if not folds:
        raise ValueError("not enough labelled signals / time span for walk-forward validation")
    parts = []
    for f in folds:
        m = make_model(kind)
        m.fit(X.iloc[f.train_idx], y.iloc[f.train_idx].astype(int))
        p = m.predict_proba(X.iloc[f.test_idx])[:, 1]
        parts.append(
            pd.DataFrame(
                dict(
                    fold=f.k, y=y.iloc[f.test_idx].astype(int).to_numpy(), p=p,
                    forward_return=meta["forward_return"].iloc[f.test_idx].to_numpy(),
                    timestamp=meta["timestamp"].iloc[f.test_idx].to_numpy(),
                    symbol=meta["symbol"].iloc[f.test_idx].to_numpy(),
                )
            )
        )
    oof = pd.concat(parts, ignore_index=True)
    res = summarize(oof, threshold)
    res["folds"] = len(folds)
    res["calibration"] = calibration_table(oof).round(4).to_dict("records")
    return res, oof, folds


def feature_importance(model, kind: str) -> pd.Series:
    if kind == "xgb":
        imp = model.feature_importances_
    else:
        imp = np.abs(model[-1].coef_[0])
    return pd.Series(imp, index=FEATURES).sort_values(ascending=False)


def train_and_save(
    engine=None, timeframe: str | None = None, kind: str = "xgb", threshold: float = 0.55,
    version: str = "v1", n_splits: int = 4, models_dir: Path | None = None,
) -> dict:
    X, y, meta = build_feature_matrix(engine, timeframe=timeframe)
    if len(X) < 100:
        raise ValueError(f"only {len(X)} labelled signals; need >=100 (backfill more history first)")
    res, oof, folds = evaluate_walk_forward(X, y, meta, kind, threshold, n_splits)

    final = make_model(kind)
    final.fit(X, y.astype(int))  # final model uses ALL labelled data
    imp = feature_importance(final, kind)

    models_dir = models_dir or MODELS_DIR
    models_dir.mkdir(parents=True, exist_ok=True)
    name = f"smc_filter_{version}"
    bundle = dict(
        model=final, features=FEATURES, threshold=threshold, version=name, kind=kind,
        trained_on=dict(n=len(X), start=str(meta["timestamp"].min()), end=str(meta["timestamp"].max()),
                        symbols=sorted(meta["symbol"].unique().tolist()), timeframe=timeframe),
        metrics={k: v for k, v in res.items() if k != "calibration"},
    )
    path = models_dir / f"{name}.pkl"
    joblib.dump(bundle, path)
    (models_dir / f"{name}.json").write_text(json.dumps(
        {**{k: v for k, v in bundle.items() if k != "model"}, "calibration": res["calibration"],
         "top_features": imp.head(10).round(4).to_dict()}, indent=2, default=str))
    _append_model_log(models_dir / "MODEL_LOG.md", bundle, res, imp)
    return dict(path=str(path), results=res, importance=imp, oof=oof)


def _append_model_log(path: Path, bundle: dict, res: dict, imp: pd.Series) -> None:
    u, f = res["unfiltered"], res["filtered"]
    t = bundle["trained_on"]
    entry = f"""
## {bundle['version']} — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC
- model: `{bundle['kind']}`, threshold {res['threshold']}, folds {res['folds']} (walk-forward, 1-day embargo)
- data: {t['n']} labelled signals, {t['start']} → {t['end']}, symbols {t['symbols']}, timeframe {t['timeframe']}
- OOS: AUC {res['auc']:.3f}, Brier {res['brier']:.3f}, base win rate {res['base_rate']:.1%}
- unfiltered: n={u['n']}, win {u['win_rate']:.1%}, avg ret {u['avg_return']:.4%}, total {u['total_return']:.2%}
- filtered:   n={f['n']}, win {f['win_rate']:.1%}, avg ret {f['avg_return']:.4%}, total {f['total_return']:.2%}
- **filter improves OOS results: {res['improves']}**
- top features: {', '.join(f'{k} ({v:.3f})' for k, v in imp.head(5).items())}
"""
    if not path.exists():
        path.write_text("# Model log\n\nEvery retrain is recorded here (data range, params, OOS results).\n")
    with path.open("a") as fh:
        fh.write(entry)


def main() -> None:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeframe", default=s.timeframe)
    p.add_argument("--model", default="xgb", choices=["xgb", "logreg"])
    p.add_argument("--threshold", type=float, default=s.min_model_probability)
    p.add_argument("--version", default="v1")
    p.add_argument("--splits", type=int, default=4)
    a = p.parse_args()
    out = train_and_save(None, a.timeframe, a.model, a.threshold, a.version, a.splits)
    r = out["results"]
    print(f"saved {out['path']}")
    print(f"OOS AUC {r['auc']:.3f} | unfiltered {r['unfiltered']['win_rate']:.1%} (n={r['unfiltered']['n']}) "
          f"| filtered {r['filtered']['win_rate']:.1%} (n={r['filtered']['n']}) | improves={r['improves']}")
    print(out["importance"].head(8).round(4).to_string())


if __name__ == "__main__":
    main()
