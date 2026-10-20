"""Walk-forward probability calibration helpers (research only, not a strategy).

Inside each FreqAI training window the classifier is fit on the EARLIER part of
the window and a Platt map (logistic regression on logit(p)) is fit on a
time-ordered holdout at the END of the same window. A purge gap of
`purge_rows` labelled rows sits between the two parts because each label looks
48h ahead. Nothing from the prediction window is used.
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression

EPS = 1e-4


def purged_holdout_split(n: int, holdout_frac: float = 0.25, purge_rows: int = 12) -> tuple[np.ndarray, np.ndarray]:
    """Time-ordered split: fit rows first, then a purge gap, then the holdout tail."""
    hold = int(round(n * holdout_frac))
    fit_end = max(n - hold - purge_rows, 0)
    return np.arange(0, fit_end), np.arange(n - hold, n)


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


class PlattMap:
    """q = sigmoid(a * logit(p) + b). Identity when the holdout has a single class.

    The slope is constrained to a > 0. A fitted slope <= 0 would REVERSE the
    model's ranking for the whole next prediction window (higher raw score ->
    lower calibrated p). In that case the map falls back to a constant: the
    training-window base rate (`base_rate`, or the holdout hit rate when not
    given). A constant p never passes a threshold above the base rate, so the
    window simply produces no entries. `fallback` records that this happened.
    """

    def __init__(self, a: float = 1.0, b: float = 0.0, fitted: bool = False, fallback: bool = False):
        self.a, self.b, self.fitted, self.fallback = float(a), float(b), fitted, fallback
        self.raw_slope = self.a      # slope the logistic fit found (kept for the audit when it fell back)

    @classmethod
    def constant(cls, p: float, raw_slope: float = 0.0) -> "PlattMap":
        out = cls(0.0, float(logit(np.array([p]))[0]), True, True)
        out.raw_slope = float(raw_slope)
        return out

    @classmethod
    def fit(cls, raw_p: np.ndarray, hit: np.ndarray, base_rate: float | None = None) -> "PlattMap":
        hit = np.asarray(hit, dtype=int)
        if len(hit) < 20 or hit.min() == hit.max():
            return cls()
        model = LogisticRegression(C=1e4, max_iter=1000)
        model.fit(logit(raw_p).reshape(-1, 1), hit)
        a, b = float(model.coef_[0, 0]), float(model.intercept_[0])
        if not a > 0:
            return cls.constant(float(hit.mean()) if base_rate is None else float(base_rate), a)
        return cls(a, b, True)

    def __call__(self, raw_p: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self.a * logit(raw_p) + self.b)))


class CalibratedModel:
    """sklearn-like wrapper FreqAI can call: predict/predict_proba return CALIBRATED values."""

    def __init__(self, base, platt: PlattMap, positive: str = "yes"):
        self.base = base
        self.platt = platt
        self.classes_ = np.asarray(base.classes_)
        self.positive = positive
        self.event_weights: dict[str, float] = {}
        self.info: dict = {}

    def _yes(self) -> int:
        return int(np.where(self.classes_ == self.positive)[0][0])

    def raw_yes(self, X) -> np.ndarray:
        return self.base.predict_proba(X)[:, self._yes()]

    def predict_proba(self, X) -> np.ndarray:
        q = self.platt(self.raw_yes(X))
        out = np.zeros((len(q), len(self.classes_)))
        yes = self._yes()
        out[:, yes] = q
        # Binary label set {"no","yes"}: the other column takes the remainder.
        out[:, 1 - yes] = 1 - q
        return out

    def predict(self, X) -> np.ndarray:
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]
