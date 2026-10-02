"""
Headline sentiment scoring.

Primary: ProsusAI/finbert via `transformers` (runs locally; first call downloads ~400MB,
so warm it up once with `python -m src.sentiment.finbert_score --warmup` before going live).

Fallback: a small finance-lexicon scorer, used ONLY if FinBERT cannot load (missing
torch/transformers, no network). It is flagged via `scorer_name()` so you can tell which ran.
Scores are signed: +1 very positive ... -1 very negative (P(pos) - P(neg)).
"""
from __future__ import annotations

import re
from typing import Callable, Sequence

Scorer = Callable[[Sequence[str]], list[tuple[str, float]]]  # texts -> [(label, signed_score)]

_scorer: Scorer | None = None
_scorer_name = "unset"


# ------------------------------------------------------------------ FinBERT
def _load_finbert() -> Scorer:
    from transformers import pipeline  # heavy import; kept lazy

    clf = pipeline("text-classification", model="ProsusAI/finbert", top_k=None, truncation=True, max_length=128)

    def score(texts: Sequence[str]) -> list[tuple[str, float]]:
        outs = clf(list(texts), batch_size=16)
        res = []
        for probs in outs:
            p = {d["label"].lower(): d["score"] for d in probs}
            signed = p.get("positive", 0.0) - p.get("negative", 0.0)
            label = max(p, key=p.get)
            res.append((label, float(signed)))
        return res

    return score


# ------------------------------------------------------------ lexicon fallback
_POS = {
    "beats", "beat", "surge", "surges", "soar", "soars", "rally", "rallies", "upgrade", "upgraded",
    "record", "growth", "profit", "profits", "gain", "gains", "strong", "bullish", "outperform",
    "raises", "raised", "approval", "approved", "wins", "win", "expands", "partnership", "breakthrough",
    "buyback", "dividend", "jumps", "climbs",
}
_NEG = {
    "miss", "misses", "missed", "plunge", "plunges", "falls", "fall", "drop", "drops", "downgrade",
    "downgraded", "loss", "losses", "lawsuit", "probe", "investigation", "recall", "weak", "bearish",
    "underperform", "cuts", "cut", "warning", "warns", "layoffs", "bankruptcy", "fraud", "delay",
    "delays", "decline", "declines", "slump", "sinks", "tumbles", "halt", "halted", "dilution",
}
_TOKEN = re.compile(r"[a-z']+")


def lexicon_scorer(texts: Sequence[str]) -> list[tuple[str, float]]:
    res = []
    for t in texts:
        words = _TOKEN.findall(t.lower())
        pos = sum(w in _POS for w in words)
        neg = sum(w in _NEG for w in words)
        total = pos + neg
        signed = 0.0 if total == 0 else (pos - neg) / (total + 1.0)
        label = "positive" if signed > 0.15 else "negative" if signed < -0.15 else "neutral"
        res.append((label, float(signed)))
    return res


# ------------------------------------------------------------------ public API
def set_scorer(scorer: Scorer | None, name: str = "custom") -> None:
    """Inject a scorer (tests, or to force a specific backend)."""
    global _scorer, _scorer_name
    _scorer, _scorer_name = scorer, name if scorer else "unset"


def _ensure_scorer() -> Scorer:
    global _scorer, _scorer_name
    if _scorer is None:
        try:
            _scorer, _scorer_name = _load_finbert(), "finbert"
        except Exception as exc:  # noqa: BLE001
            print(f"[sentiment] FinBERT unavailable ({exc!s:.120}); using lexicon fallback")
            _scorer, _scorer_name = lexicon_scorer, "lexicon-fallback"
    return _scorer


def scorer_name() -> str:
    return _scorer_name


def score_headlines(texts: Sequence[str]) -> list[tuple[str, float]]:
    """[(label, signed_score)] for each text."""
    if not texts:
        return []
    return _ensure_scorer()(list(texts))


def score_headline(text: str) -> tuple[str, float]:
    """(label, signed_score) for one headline. Label in {positive, neutral, negative}."""
    return score_headlines([text])[0]


if __name__ == "__main__":
    import sys

    if "--warmup" in sys.argv:
        print(score_headline("Company beats earnings expectations and raises guidance"), scorer_name())
