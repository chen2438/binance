"""Point-in-time symbol screening.

Screening is where look-ahead bias is easiest to introduce and hardest to notice:
ranking symbols with statistics computed over the whole history produces a backtest
that cannot be reproduced live. Everything here is built so a rule can only ever see
data that existed at the moment it fires.
"""

from .cross import long_short_pool
from .features import FEATURES, compute_features
from .panel import build_panel
from .screener import Screener, ScreenRule, top_n

__all__ = [
    "FEATURES",
    "long_short_pool",
    "Screener",
    "ScreenRule",
    "build_panel",
    "compute_features",
    "top_n",
]
