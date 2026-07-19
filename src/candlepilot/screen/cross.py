"""Cross-sectional signals: rank symbols against each other at each rebalance.

A time-series signal asks "is this symbol going up?", which means every position
carries the whole market's direction. A cross-sectional signal asks "is this symbol
going up *relative to its peers*?", and pairing the top against the bottom cancels
most of that shared direction. In a market where nearly everything moves together —
crypto perps especially — that difference is usually larger than the signal itself.

The signal lives here rather than in a strategy because it is a ranking over the
point-in-time feature panel, which is exactly what the screening layer already
computes. Execution then only needs to follow the side it is assigned, so the
look-ahead guarantees of ``compute_features`` carry over unchanged.
"""

from __future__ import annotations

import pandas as pd

from .screener import Screener


def long_short_pool(
    features: pd.DataFrame,
    column: str,
    *,
    n: int = 5,
    rebalance: str = "W-MON",
    filters: dict[str, tuple[str, float]] | None = None,
    reverse: bool = False,
) -> pd.DataFrame:
    """Long the top ``n`` by ``column``, short the bottom ``n``.

    ``reverse=True`` flips the assignment, which turns a momentum ranking into a
    reversal one without touching the feature.

    Returns a pool frame with ``date``, ``symbol``, ``side`` (+1/-1) and ``rank``.
    """
    operators = {
        ">=": lambda s, v: s >= v,
        ">": lambda s, v: s > v,
        "<=": lambda s, v: s <= v,
        "<": lambda s, v: s < v,
    }

    screener = Screener(lambda snapshot: [], rebalance=rebalance)
    rows = []

    for date in screener.rebalance_dates(features):
        snapshot = features.xs(date, level="date", drop_level=False)
        eligible = snapshot[snapshot["eligible"]]
        for name, (operator, value) in (filters or {}).items():
            if name not in eligible.columns:
                raise KeyError(f"filter column {name!r} is not a computed feature")
            eligible = eligible[operators[operator](eligible[name], value)]

        # Both legs need enough names to be a ranking rather than a coin flip.
        if len(eligible) < 2 * n:
            continue

        ranked = eligible.sort_values(column, ascending=False)
        symbols = ranked.index.get_level_values("symbol")
        top, bottom = symbols[:n], symbols[-n:]
        long_leg, short_leg = (bottom, top) if reverse else (top, bottom)

        for rank, symbol in enumerate(long_leg):
            rows.append({"date": date, "symbol": symbol, "side": 1, "rank": rank})
        for rank, symbol in enumerate(short_leg):
            rows.append({"date": date, "symbol": symbol, "side": -1, "rank": rank})

    return pd.DataFrame(rows, columns=["date", "symbol", "side", "rank"])
