"""Evaluation and overfitting control.

Built before rule search begins, deliberately. Once you have seen an in-sample
result, every later choice — which split, which objective, which correction — is
made by someone who already knows the answer, and the correction stops being one.
"""

from .significance import (
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    sharpe_ratio,
)
from .splits import Split, walk_forward
from .sweep import SweepResult, WalkForwardResult, run_walk_forward, sweep_parameters

__all__ = [
    "Split",
    "SweepResult",
    "WalkForwardResult",
    "deflated_sharpe",
    "expected_max_sharpe",
    "probabilistic_sharpe",
    "run_walk_forward",
    "sharpe_ratio",
    "sweep_parameters",
    "walk_forward",
]
