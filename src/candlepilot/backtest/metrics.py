"""Performance metrics and the cost sweep.

``sweep_costs`` is the headline entry point: it reruns one strategy across the cost
scenarios and reports where the edge dies. A single-cost backtest cannot answer that,
and for intraday work the answer is usually "between optimistic and base".
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .costs import COST_SCENARIOS, CostModel
from .engine import Backtest, BacktestResult, Strategy

# 1m bars; a year of continuous trading.
BARS_PER_YEAR = 365 * 24 * 60


@dataclass
class Metrics:
    total_return: float
    max_drawdown: float
    sharpe: float
    trades: int
    win_rate: float
    profit_factor: float
    total_fees: float
    total_funding: float
    liquidations: int

    def as_row(self) -> dict:
        return self.__dict__.copy()


def summarize(result: BacktestResult) -> Metrics:
    curve = result.equity_curve
    start = result.initial_equity

    total_return = curve.iloc[-1] / start - 1 if len(curve) else 0.0
    drawdown = (curve / curve.cummax() - 1).min() if len(curve) else 0.0

    returns = curve.pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        sharpe = returns.mean() / returns.std() * (BARS_PER_YEAR**0.5)
    else:
        sharpe = 0.0

    trades = result.trades
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)

    return Metrics(
        total_return=float(total_return),
        max_drawdown=float(drawdown),
        sharpe=float(sharpe),
        trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else float("inf"),
        total_fees=sum(t.fees for t in trades),
        total_funding=sum(t.funding for t in trades),
        liquidations=result.liquidations,
    )


def sweep_costs(
    frame: pd.DataFrame,
    strategy_factory,
    *,
    symbol: str = "",
    scenarios: dict[str, CostModel] | None = None,
    **backtest_kwargs,
) -> pd.DataFrame:
    """Run the same strategy under each cost scenario.

    ``strategy_factory`` is called per scenario so a stateful strategy cannot leak
    state between runs.
    """
    scenarios = scenarios or COST_SCENARIOS
    rows = []
    for name, costs in scenarios.items():
        backtest = Backtest(frame, symbol=symbol, cost_model=costs, **backtest_kwargs)
        result = backtest.run(strategy_factory())
        row = summarize(result).as_row()
        row["scenario"] = name
        row["round_trip_cost"] = costs.round_trip
        rows.append(row)

    columns = ["scenario", "round_trip_cost"] + [
        c for c in rows[0] if c not in ("scenario", "round_trip_cost")
    ]
    return pd.DataFrame(rows).loc[:, columns]
