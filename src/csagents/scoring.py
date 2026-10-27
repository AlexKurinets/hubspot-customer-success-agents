"""The health score: four signals, each worth a fixed number of points, computed in code every night.

The points come from a logistic regression on past renewals (`fit_weights`), rescaled to sum to 100
and rounded to fives so a CSM can check a score in their head. The model never sets a score; it only
tags the reasons behind one (an early version that let it score moved identical accounts by up to
15 points between runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from .sources.billing import BillingState
from .sources.warehouse import UsageWeek

FEATURES = ("seat_utilization", "module_adoption", "ticket_ok", "invoice_current")
SEVERITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 4}
TICKET_CAP = 12


@dataclass(frozen=True)
class Health:
    score: int
    tier: str
    features: dict[str, float]
    points: dict[str, float]


def features(usage: list[UsageWeek], ticket_priorities: list[str], billing: BillingState | None) -> dict[str, float]:
    if usage:
        utilization = min(1.0, sum(w.utilization for w in usage) / len(usage))
        modules = (any(w.appointments for w in usage) + any(w.records for w in usage)
                   + any(w.invoicing for w in usage)) / 3
    else:
        utilization, modules = 0.0, 0.0
    severity = sum(SEVERITY.get(p, 1) for p in ticket_priorities)
    return {
        "seat_utilization": round(utilization, 4),
        "module_adoption": round(modules, 4),
        "ticket_ok": round(1 - min(1.0, severity / TICKET_CAP), 4),
        "invoice_current": 1.0 if billing is None or billing.invoice_current else 0.0,
    }


def score(feats: dict[str, float], weights: dict[str, int], tier_of) -> Health:
    points = {k: round(weights[k] * feats[k], 1) for k in FEATURES}
    total = int(round(sum(points.values())))
    return Health(total, tier_of(total), feats, points)


# ---------------------------------------------------------------- fitting


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def logistic_fit(X: np.ndarray, y: np.ndarray, iters: int = 6000, lr: float = 0.5) -> np.ndarray:
    """Plain batch gradient descent; deterministic, and enough for four features."""
    Xb = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        w -= lr * Xb.T @ (_sigmoid(Xb @ w) - y) / len(y)
    return w


def auc(scores: np.ndarray, positives: np.ndarray) -> float:
    """Probability that a random positive outranks a random negative (ties count half)."""
    pos, neg = scores[positives], scores[~positives]
    greater = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * ties) / (len(pos) * len(neg)))


def to_points(coefficients: np.ndarray) -> dict[str, int]:
    raw = 100 * coefficients / coefficients.sum()
    points = {k: int(5 * round(v / 5)) for k, v in zip(FEATURES, raw, strict=True)}
    # Rounding can leave the total at 95 or 105; move the difference onto the largest weight.
    biggest = max(points, key=points.get)
    points[biggest] += 100 - sum(points.values())
    return points


def fit_weights(history: list[dict], holdout_from: date) -> dict:
    """Fits on renewals before `holdout_from`, measures on the renewals after it."""
    X = np.array([[r[k] for k in FEATURES] for r in history], dtype=float)
    churned = np.array([r["churned"] for r in history], dtype=bool)
    train = np.array([r["renewal_date"] < holdout_from for r in history])
    coef = logistic_fit(X[train], (~churned[train]).astype(float))
    points = to_points(coef[1:])
    test = ~train
    rounded = X[test] @ np.array([points[k] for k in FEATURES])
    raw = X[test] @ coef[1:]
    return {
        "points": points,
        "coefficients": {k: round(float(c), 3) for k, c in zip(FEATURES, coef[1:], strict=True)},
        "trained_on": int(train.sum()),
        "held_out": int(test.sum()),
        "held_out_churned": int(churned[test].sum()),
        # Healthier accounts score higher, so a churned account "outranks" when its score is lower.
        "held_out_auc": round(auc(-rounded, churned[test]), 3),
        "held_out_auc_unrounded": round(auc(-raw, churned[test]), 3),
    }
