"""Significance testing that accounts for how many strategies were tried.

Searching a parameter grid guarantees a good-looking best result even when no
parameterisation has any edge: the maximum of N noisy Sharpe ratios grows with N.
Reporting that maximum as if it were a single hypothesis is the central overfitting
error in strategy research, and no amount of out-of-sample testing repairs it if the
out-of-sample period was itself used to choose.

Implemented here, following Bailey & López de Prado:

* **PSR** — Probabilistic Sharpe Ratio: probability the true Sharpe exceeds a
  benchmark, correcting for sample length, skew and fat tails. Trading returns are
  neither normal nor independent, and a plain Sharpe silently assumes both.
* **E[max SR]** — the Sharpe the *best* of N trials would reach by luck alone.
* **DSR** — Deflated Sharpe Ratio: PSR benchmarked against E[max SR], i.e. the
  probability the winner beats what luck would have produced anyway.

All functions take returns at their native frequency and use a Sharpe at that same
frequency. Annualising before these tests inflates the statistic without adding
observations, which is precisely the error they exist to catch.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_NORMAL = NormalDist()
EULER_MASCHERONI = 0.5772156649015329


def sharpe_ratio(returns: pd.Series | np.ndarray, *, periods_per_year: int | None = None) -> float:
    """Sharpe at the returns' own frequency, or annualised if asked explicitly."""
    values = np.asarray(returns, dtype="float64")
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return float("nan")
    deviation = values.std(ddof=1)
    if deviation == 0:
        return float("nan")
    sharpe = values.mean() / deviation
    if periods_per_year is not None:
        sharpe *= math.sqrt(periods_per_year)
    return float(sharpe)


def probabilistic_sharpe(
    returns: pd.Series | np.ndarray,
    *,
    benchmark: float = 0.0,
) -> float:
    """P(true Sharpe > ``benchmark``), adjusted for sample size, skew and kurtosis.

    ``benchmark`` is a Sharpe at the same frequency as ``returns``.
    """
    values = np.asarray(returns, dtype="float64")
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 3:
        return float("nan")

    observed = sharpe_ratio(values)
    if not math.isfinite(observed):
        return float("nan")

    centred = values - values.mean()
    deviation = values.std(ddof=1)
    if deviation == 0:
        return float("nan")
    skew = float((centred**3).mean() / deviation**3)
    # Non-excess kurtosis: 3.0 for a normal distribution.
    kurtosis = float((centred**4).mean() / deviation**4)

    variance = 1.0 - skew * observed + 0.25 * (kurtosis - 1.0) * observed**2
    if variance <= 0:
        # Extreme higher moments can drive the estimator's variance non-positive;
        # reporting a confident probability from it would be spurious.
        return float("nan")

    statistic = (observed - benchmark) * math.sqrt(n - 1) / math.sqrt(variance)
    return float(_NORMAL.cdf(statistic))


def expected_max_sharpe(trial_sharpes: pd.Series | np.ndarray | None = None, *,
                        n_trials: int | None = None,
                        sharpe_variance: float | None = None) -> float:
    """Sharpe the best of N independent trials reaches under a zero-edge null.

    Supply either the Sharpe ratios of every trial, or ``n_trials`` together with
    ``sharpe_variance``. This is the bar a search's winner must clear to mean
    anything.
    """
    if trial_sharpes is not None:
        values = np.asarray(trial_sharpes, dtype="float64")
        values = values[np.isfinite(values)]
        n_trials = len(values)
        sharpe_variance = float(values.var(ddof=1)) if n_trials > 1 else 0.0

    if not n_trials or n_trials < 1 or sharpe_variance is None:
        return float("nan")
    if n_trials == 1 or sharpe_variance <= 0:
        return 0.0

    deviation = math.sqrt(sharpe_variance)
    # Expected maximum of N standard normals (Gumbel approximation).
    first = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return float(deviation * ((1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second))


def deflated_sharpe(
    returns: pd.Series | np.ndarray,
    *,
    trial_sharpes: pd.Series | np.ndarray | None = None,
    n_trials: int | None = None,
    sharpe_variance: float | None = None,
) -> float:
    """P(the winning strategy's true Sharpe beats what N trials would find by luck).

    Below ~0.95 the result is not distinguishable from search luck.
    """
    benchmark = expected_max_sharpe(
        trial_sharpes, n_trials=n_trials, sharpe_variance=sharpe_variance
    )
    if not math.isfinite(benchmark):
        return float("nan")
    return probabilistic_sharpe(returns, benchmark=benchmark)


def minimum_track_record_length(
    returns: pd.Series | np.ndarray,
    *,
    benchmark: float = 0.0,
    confidence: float = 0.95,
) -> float:
    """Observations needed before the Sharpe could be called significant.

    Answers "is this backtest even long enough to support the claim?" — a question
    that a headline Sharpe never raises on its own.
    """
    values = np.asarray(returns, dtype="float64")
    values = values[np.isfinite(values)]
    if len(values) < 3:
        return float("nan")

    observed = sharpe_ratio(values)
    if not math.isfinite(observed) or observed <= benchmark:
        return float("inf")

    centred = values - values.mean()
    deviation = values.std(ddof=1)
    skew = float((centred**3).mean() / deviation**3)
    kurtosis = float((centred**4).mean() / deviation**4)

    variance = 1.0 - skew * observed + 0.25 * (kurtosis - 1.0) * observed**2
    if variance <= 0:
        return float("nan")

    z = _NORMAL.inv_cdf(confidence)
    return float(1.0 + variance * (z / (observed - benchmark)) ** 2)
