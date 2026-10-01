from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.data_ingestion.backfill import save_bars
from src.data_ingestion.synthetic import make_bars
from src.db.schema import SentimentScore, get_engine, init_db, session_scope
from src.ml import predict as predict_mod
from src.ml.features import FEATURES, build_feature_matrix, row_features
from src.ml.train import (
    evaluate_walk_forward,
    summarize,
    train_and_save,
    walk_forward_splits,
)
from src.smc_logic.backfill_signals import backfill_signals


@pytest.fixture(scope="module")
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        save_bars(eng, sym, "15Min", make_bars(n_days=260, seed=40 + i, start_price=50 + 20 * i))
        backfill_signals([sym], "15Min", horizon=8, engine=eng)
    return eng


def _details(**kw):
    base = dict(
        rsi=60.0, vol_ratio=1.5, ema_gap_pct=0.002, atr_pct=0.004, range_atr=1.1, swing_trend=1,
        internal_trend=1, last_swing_dir=1, last_swing_is_choch=False, bars_since_swing_event=12,
        last_int_dir=1, last_int_is_choch=True, bars_since_int_event=3, pd_position=0.3,
        in_bull_ob=True, in_bear_ob=False, dist_bull_ob_atr=0.4, dist_bear_ob_atr=2.5,
        ob_bull_count=2, ob_bear_count=1, fvg_bull_active=1, fvg_bear_active=0,
        in_bull_fvg=False, in_bear_fvg=False, zone="discount",
    )
    base.update(kw)
    return base


# ----------------------------------------------------------------- features
def test_features_direction_alignment_flips_for_shorts():
    ts = datetime(2026, 6, 2, 14, 45)  # 10:45 ET
    long_f = row_features(_details(), "long", ts)
    short_f = row_features(_details(), "short", ts)
    assert list(long_f) == FEATURES == list(short_f)
    assert long_f["swing_aligned"] == 1 and short_f["swing_aligned"] == -1
    assert long_f["pd_edge"] == pytest.approx(0.2) and short_f["pd_edge"] == pytest.approx(-0.2)
    assert long_f["in_ob_support"] == 1.0 and short_f["in_ob_oppose"] == 1.0
    assert long_f["ob_dist_support_atr"] == 0.4 and short_f["ob_dist_support_atr"] == 2.5
    assert long_f["zone_discount"] == 1.0
    assert long_f["minutes_since_open"] == 75 and long_f["day_of_week"] == 1.0


def test_features_handle_missing_values():
    f = row_features({}, "long", datetime(2026, 6, 2, 14, 45), None)
    assert np.isnan(f["rsi"]) and f["bars_since_swing_event"] == 500.0
    assert f["sent_n"] == 0.0 and f["sent_aligned"] == 0.0


def test_sentiment_features_are_aligned():
    f = row_features(_details(), "short", datetime(2026, 6, 2, 14, 45), dict(score=-0.6, n=3))
    assert f["sent_aligned"] == pytest.approx(0.6) and f["sent_n"] == 3  # negative news supports a short


def test_feature_matrix_from_db(engine):
    X, y, meta = build_feature_matrix(engine, timeframe="15Min")
    assert list(X.columns) == FEATURES and len(X) == len(y) == len(meta) > 200
    assert set(y.dropna().unique()) <= {0, 1}
    assert (meta["timestamp"].diff().dropna() >= timedelta(0)).all()  # time-ordered


def test_feature_matrix_picks_up_sentiment(engine):
    X0, _, meta = build_feature_matrix(engine, timeframe="15Min")
    ts, sym = meta["timestamp"].iloc[50], meta["symbol"].iloc[50]
    with session_scope(engine) as s:
        s.add(SentimentScore(symbol=sym, timestamp=ts - timedelta(minutes=30), score=0.9, label="positive"))
    X1, _, _ = build_feature_matrix(engine, timeframe="15Min")
    assert X1["sent_n"].iloc[50] == 1 and X0["sent_n"].iloc[50] == 0
    with session_scope(engine) as s:  # clean up so later tests are unaffected
        s.query(SentimentScore).delete()


# ------------------------------------------------------------ walk-forward
def test_walk_forward_has_no_leakage():
    ts = pd.Series(pd.date_range("2026-01-05", periods=800, freq="2h"))
    folds = walk_forward_splits(ts, n_splits=4, embargo_days=1.0)
    assert len(folds) == 4
    for f in folds:
        assert ts.iloc[f.train_idx].max() < ts.iloc[f.test_idx].min() - pd.Timedelta(days=1) + pd.Timedelta(seconds=1)
        assert set(f.train_idx).isdisjoint(f.test_idx)
    # test windows are disjoint, ordered, and the training set only ever grows
    for a, b in zip(folds, folds[1:]):
        assert ts.iloc[a.test_idx].max() < ts.iloc[b.test_idx].min()
        assert len(b.train_idx) > len(a.train_idx)


def test_walk_forward_raises_without_data():
    X = pd.DataFrame(np.zeros((10, len(FEATURES))), columns=FEATURES)
    meta = pd.DataFrame(dict(timestamp=pd.date_range("2026-01-05", periods=10, freq="1h"),
                             symbol="A", forward_return=0.0))
    with pytest.raises(ValueError):
        evaluate_walk_forward(X, pd.Series([0, 1] * 5), meta)


def _planted_edge(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=FEATURES)
    logit = 2.0 * X["swing_aligned"] - 1.5 * X["pd_edge"]
    y = pd.Series((rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int))
    ret = np.where(y == 1, 0.004, -0.003) + rng.normal(0, 0.001, n)
    meta = pd.DataFrame(dict(timestamp=pd.date_range("2026-01-05", periods=n, freq="3h"),
                             symbol="A", forward_return=ret, direction="long"))
    return X, y, meta


@pytest.mark.parametrize("kind", ["xgb", "logreg"])
def test_model_machinery_learns_a_planted_edge(kind):
    X, y, meta = _planted_edge()
    res, oof, _ = evaluate_walk_forward(X, y, meta, kind=kind, threshold=0.6)
    assert res["auc"] > 0.75
    assert res["filtered"]["win_rate"] > res["unfiltered"]["win_rate"] + 0.1
    assert res["improves"] is True


def test_model_finds_no_edge_in_pure_noise():
    rng = np.random.default_rng(3)
    n = 1500
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=FEATURES)
    y = pd.Series(rng.integers(0, 2, n))
    meta = pd.DataFrame(dict(timestamp=pd.date_range("2026-01-05", periods=n, freq="3h"),
                             symbol="A", forward_return=rng.normal(0, 0.003, n), direction="long"))
    res, _, _ = evaluate_walk_forward(X, y, meta, kind="xgb", threshold=0.55)
    assert 0.42 < res["auc"] < 0.58  # honest: no leakage manufacturing a fake edge


def test_summarize_metrics_are_consistent():
    oof = pd.DataFrame(dict(y=[1, 1, 0, 0, 1, 0], p=[0.9, 0.8, 0.7, 0.2, 0.6, 0.1],
                            forward_return=[0.02, 0.01, -0.01, -0.01, 0.03, -0.02]))
    r = summarize(oof, 0.5)
    assert r["filtered"]["n"] == 4 and r["filtered"]["win_rate"] == 0.75
    assert r["unfiltered"]["n"] == 6 and r["unfiltered"]["win_rate"] == 0.5
    assert r["precision"] == 0.75 and r["recall"] == 1.0
    assert r["filtered"]["profit_factor"] == pytest.approx(0.06 / 0.01)


# -------------------------------------------------------------- end to end
def test_train_save_load_predict_roundtrip(engine, tmp_path):
    out = train_and_save(engine, "15Min", kind="logreg", version="test", n_splits=3, models_dir=tmp_path)
    assert (tmp_path / "smc_filter_test.pkl").exists() and (tmp_path / "MODEL_LOG.md").exists()
    assert "smc_filter_test" in (tmp_path / "MODEL_LOG.md").read_text()
    bundle = predict_mod.load_bundle(out["path"])
    assert bundle["features"] == FEATURES and bundle["version"] == "smc_filter_test"
    p = predict_mod.predict_probability(bundle, row_features(_details(), "long", datetime(2026, 6, 2, 14, 45)))
    assert 0.0 <= p <= 1.0
    assert predict_mod.latest_model_path(tmp_path).name == "smc_filter_test.pkl"


def test_load_bundle_returns_none_without_model(tmp_path):
    assert predict_mod.latest_model_path(tmp_path) is None
    assert predict_mod.load_bundle(tmp_path / "nope.pkl") is None
