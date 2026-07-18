"""Backtest engine for USDT-M perpetual futures.

Backtest only: nothing here places orders or touches an account.
"""

from .costs import COST_SCENARIOS, CostModel
from .dataset import build_bars
from .engine import Backtest, Intent, Strategy
from .metrics import Metrics, summarize, sweep_costs
from .position import Position, size_for_risk

__all__ = [
    "COST_SCENARIOS",
    "Backtest",
    "CostModel",
    "Intent",
    "Metrics",
    "Position",
    "Strategy",
    "build_bars",
    "size_for_risk",
    "summarize",
    "sweep_costs",
]
