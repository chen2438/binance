"""Strategies. Nothing here is a researched edge — see module docstrings."""

from .baselines import BASELINES, FundingCarry, MeanReversion, MomentumContinuation
from .reference import DonchianBreakout

__all__ = [
    "BASELINES",
    "DonchianBreakout",
    "FundingCarry",
    "MeanReversion",
    "MomentumContinuation",
]
