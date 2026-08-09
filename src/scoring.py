"""Scoring: Brier score, log loss, calibration, bootstrap CI on the edge.

Pure stdlib — no numpy/pandas dependency, so the go/no-go test has nothing
else that can be wrong.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Mean squared error between predicted probability and actual outcome (0/1). Lower is better."""
    n = len(predictions)
    if n == 0:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / n


def log_loss(predictions: list[float], outcomes: list[int], eps: float = 1e-15) -> float:
    """Mean binary cross-entropy. Punishes confident-and-wrong harder than Brier."""
    n = len(predictions)
    if n == 0:
        return float("nan")
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = min(max(p, eps), 1 - eps)
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / n


@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_predicted: float | None
    mean_actual: float | None


@dataclass
class Calibration:
    bins: list[CalibrationBin] = field(default_factory=list)
    ece: float = float("nan")  # expected calibration error


def calibration(predictions: list[float], outcomes: list[int], n_bins: int = 10) -> Calibration:
    """Bucket predictions into n_bins and compare mean predicted prob to actual frequency.
    A well-calibrated forecaster's bins fall on the y=x diagonal.
    """
    edges = [i / n_bins for i in range(n_bins + 1)]
    bins: list[CalibrationBin] = []
    n_total = len(predictions)
    weighted_error = 0.0

    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        idx = [
            j
            for j, p in enumerate(predictions)
            if (lo <= p < hi) or (i == n_bins - 1 and p == hi)
        ]
        if not idx:
            bins.append(CalibrationBin(lo, hi, 0, None, None))
            continue
        mean_pred = sum(predictions[j] for j in idx) / len(idx)
        mean_actual = sum(outcomes[j] for j in idx) / len(idx)
        bins.append(CalibrationBin(lo, hi, len(idx), mean_pred, mean_actual))
        weighted_error += abs(mean_pred - mean_actual) * (len(idx) / n_total)

    return Calibration(bins=bins, ece=weighted_error)


def bootstrap_brier_gap(
    ai_predictions: list[float],
    market_predictions: list[float],
    outcomes: list[int],
    n_boot: int = 2000,
    ci: float = 0.90,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Paired bootstrap CI on (market_brier - ai_brier) per resample.

    Positive gap means the AI beat the market on that resample. Returns
    (point_estimate, ci_lo, ci_hi).
    """
    n = len(outcomes)
    rng = random.Random(seed)
    point = brier_score(market_predictions, outcomes) - brier_score(ai_predictions, outcomes)

    if n == 0:
        return point, float("nan"), float("nan")

    gaps = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        ai_p = [ai_predictions[i] for i in idx]
        mk_p = [market_predictions[i] for i in idx]
        out = [outcomes[i] for i in idx]
        gaps.append(brier_score(mk_p, out) - brier_score(ai_p, out))

    gaps.sort()
    lo_idx = int((1 - ci) / 2 * n_boot)
    hi_idx = int((1 - (1 - ci) / 2) * n_boot) - 1
    hi_idx = min(hi_idx, n_boot - 1)
    return point, gaps[lo_idx], gaps[hi_idx]


def verdict(ci_lo: float, ci_hi: float) -> str:
    """Turn a bootstrap CI on the Brier gap into a plain-English go/no-go read."""
    if ci_lo > 0:
        return "REAL EDGE — CI entirely above 0, the AI's win is unlikely to be noise."
    if ci_hi < 0:
        return "NO EDGE — CI entirely below 0, the crowd is beating the AI as built."
    return "INCONCLUSIVE — CI straddles 0. Get more questions (aim for 100-200+)."
