"""Parameter sweeps under walk-forward discipline.

The distinction this module enforces: parameters are chosen on **training** data
only, and the reported result comes from **test** data the chooser never saw. A
sweep that reports its own best in-sample number is measuring the grid's size, not
the strategy.

Every trial's Sharpe is retained, not just the winner's, because the spread across
trials is the input to ``expected_max_sharpe`` — without it there is no way to say
how good the best result should have looked by luck alone.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest.engine import Backtest, BacktestResult
from ..backtest.metrics import summarize
from .significance import deflated_sharpe, expected_max_sharpe, probabilistic_sharpe, sharpe_ratio
from .splits import Split, walk_forward

log = logging.getLogger("candlepilot.evaluate")


def parameter_grid(space: dict[str, Iterable]) -> list[dict]:
    """Expand a parameter space into a list of concrete parameter dicts."""
    keys = list(space)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(space[k] for k in keys))]


@dataclass
class SweepResult:
    """Every trial's outcome over one window."""

    table: pd.DataFrame
    objective: str

    @property
    def best(self) -> dict:
        if self.table.empty:
            return {}
        row = self.table.loc[self.table[self.objective].idxmax()]
        return {k: row[k] for k in self.table.columns if k.startswith("param_")}

    @property
    def best_params(self) -> dict:
        return {k.removeprefix("param_"): v for k, v in self.best.items()}


@dataclass
class WalkForwardResult:
    splits: list[Split]
    folds: pd.DataFrame
    oos_returns: pd.Series
    trial_sharpes: np.ndarray
    n_trials: int = 0
    equity_curve: pd.Series = field(default_factory=pd.Series)

    def report(self) -> dict:
        """Headline out-of-sample numbers, with the search cost priced in."""
        returns = self.oos_returns.dropna()
        observed = sharpe_ratio(returns)
        benchmark = expected_max_sharpe(
            self.trial_sharpes if len(self.trial_sharpes) else None,
            n_trials=self.n_trials or None,
            sharpe_variance=0.0 if not len(self.trial_sharpes) else None,
        )
        return {
            "folds": len(self.splits),
            "oos_observations": int(len(returns)),
            "oos_sharpe": observed,
            "trials_per_fold": self.n_trials,
            "expected_max_sharpe_under_null": benchmark,
            "psr_vs_zero": probabilistic_sharpe(returns),
            "deflated_sharpe": deflated_sharpe(
                returns, trial_sharpes=self.trial_sharpes if len(self.trial_sharpes) else None
            ),
        }


def sweep_parameters(
    frame: pd.DataFrame,
    strategy_factory: Callable[..., object],
    space: dict[str, Iterable],
    *,
    objective: str = "sharpe",
    symbol: str = "",
    **backtest_kwargs,
) -> SweepResult:
    """Run every parameter combination over one window and tabulate the results."""
    rows = []
    for params in parameter_grid(space):
        result = Backtest(frame, symbol=symbol, **backtest_kwargs).run(
            strategy_factory(**params)
        )
        metrics = summarize(result).as_row()
        metrics.update({f"param_{k}": v for k, v in params.items()})
        metrics["bar_sharpe"] = sharpe_ratio(result.equity_curve.pct_change().dropna())
        rows.append(metrics)

    table = pd.DataFrame(rows)
    return SweepResult(table=table, objective=objective)


def run_walk_forward(
    frame: pd.DataFrame,
    strategy_factory: Callable[..., object],
    space: dict[str, Iterable],
    *,
    train: str = "90D",
    test: str = "30D",
    embargo: str = "1D",
    anchored: bool = False,
    objective: str = "sharpe",
    symbol: str = "",
    **backtest_kwargs,
) -> WalkForwardResult:
    """Choose parameters on each training window, then measure on the next test window.

    The returned Sharpe is stitched from test windows only. ``trial_sharpes`` collects
    every training-window trial so the result can be deflated by how hard the search
    looked.
    """
    splits = walk_forward(
        frame.index, train=train, test=test, embargo=embargo, anchored=anchored
    )
    if not splits:
        raise ValueError(
            "no walk-forward folds fit in this data; shorten train/test or extend the range"
        )

    grid = parameter_grid(space)
    fold_rows = []
    oos_pieces: list[pd.Series] = []
    all_trial_sharpes: list[float] = []

    for number, split in enumerate(splits, 1):
        # Masks, not .loc slicing: label slicing is end-inclusive, which would put the
        # boundary bar in both windows and hand one training bar to the test set.
        train_frame = frame[split.train_mask(frame.index)]
        test_frame = frame[split.test_mask(frame.index)]
        if train_frame.empty or test_frame.empty:
            continue

        best_score = -np.inf
        best_params: dict = {}
        for params in grid:
            result = Backtest(train_frame, symbol=symbol, **backtest_kwargs).run(
                strategy_factory(**params)
            )
            score = _score(result, objective)
            all_trial_sharpes.append(
                sharpe_ratio(result.equity_curve.pct_change().dropna())
            )
            if np.isfinite(score) and score > best_score:
                best_score, best_params = score, params

        if not best_params:
            log.warning("fold %d: no parameterisation produced a finite score", number)
            continue

        # The only run that counts: chosen parameters, unseen window.
        oos = Backtest(test_frame, symbol=symbol, **backtest_kwargs).run(
            strategy_factory(**best_params)
        )
        oos_metrics = summarize(oos)
        oos_pieces.append(oos.equity_curve.pct_change().dropna())

        fold_rows.append(
            {
                "fold": number,
                "train_start": split.train_start,
                "test_start": split.test_start,
                "test_end": split.test_end,
                "is_score": best_score,
                "oos_return": oos_metrics.total_return,
                "oos_sharpe": oos_metrics.sharpe,
                "oos_trades": oos_metrics.trades,
                "oos_liquidations": oos_metrics.liquidations,
                **{f"param_{k}": v for k, v in best_params.items()},
            }
        )

    oos_returns = (
        pd.concat(oos_pieces) if oos_pieces else pd.Series(dtype="float64")
    )
    return WalkForwardResult(
        splits=splits,
        folds=pd.DataFrame(fold_rows),
        oos_returns=oos_returns,
        trial_sharpes=np.asarray(all_trial_sharpes, dtype="float64"),
        n_trials=len(grid),
        equity_curve=(1.0 + oos_returns).cumprod() if len(oos_returns) else pd.Series(dtype="float64"),
    )


def _score(result: BacktestResult, objective: str) -> float:
    metrics = summarize(result)
    if objective == "sharpe":
        return metrics.sharpe
    if objective == "total_return":
        return metrics.total_return
    if objective == "profit_factor":
        return metrics.profit_factor
    if objective == "calmar":
        drawdown = abs(metrics.max_drawdown)
        return metrics.total_return / drawdown if drawdown > 0 else float("-inf")
    raise ValueError(f"unknown objective {objective!r}")
