"""Rolling universe construction.

The screener answers one question repeatedly: *given only what was known on this
date, which symbols would the rule have picked?* Rebalance dates partition time, and
a pool selected on date ``T`` governs trading until the next rebalance — so a symbol
that delists at ``T+3`` is still legitimately in the pool chosen at ``T``. Excluding
it would be hindsight, and it is precisely the exclusion that inflates backtests.

Delisting needs no special handling on the way out: a delisted symbol simply has no
panel rows after its last trading day, so it cannot be selected at any later
rebalance. The portfolio layer is what must cope with a held symbol disappearing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

# A rule receives one rebalance date's eligible cross-section and returns symbols.
ScreenRule = Callable[[pd.DataFrame], list[str]]


def top_n(
    column: str,
    n: int = 20,
    *,
    ascending: bool = False,
    filters: dict[str, tuple[str, float]] | None = None,
) -> ScreenRule:
    """Rank on one column, optionally after threshold filters.

    ``filters`` maps a column to ``(operator, value)`` with operator in
    ``>= > <= <``, e.g. ``{"liquidity": (">=", 1e6)}``.
    """
    operators = {
        ">=": lambda s, v: s >= v,
        ">": lambda s, v: s > v,
        "<=": lambda s, v: s <= v,
        "<": lambda s, v: s < v,
    }

    def rule(snapshot: pd.DataFrame) -> list[str]:
        frame = snapshot
        for name, (operator, value) in (filters or {}).items():
            if name not in frame.columns:
                raise KeyError(f"filter column {name!r} is not a computed feature")
            frame = frame[operators[operator](frame[name], value)]
        if frame.empty:
            return []
        ranked = frame.sort_values(column, ascending=ascending)
        return ranked.index.get_level_values("symbol")[:n].tolist()

    rule.__name__ = f"top_{n}_{column}"
    return rule


@dataclass
class Selection:
    date: pd.Timestamp
    symbols: list[str]
    candidates: int


class Screener:
    """Applies a rule at each rebalance date over a feature panel."""

    def __init__(self, rule: ScreenRule, *, rebalance: str = "W-MON"):
        self.rule = rule
        self.rebalance = rebalance

    def rebalance_dates(self, features: pd.DataFrame) -> pd.DatetimeIndex:
        dates = features.index.get_level_values("date").unique().sort_values()
        if self.rebalance in ("D", "1D"):
            return dates
        # Snap each period to a date that actually exists in the panel; a rebalance
        # on a date with no data would silently select nothing.
        periods = pd.Series(dates, index=dates).resample(self.rebalance).first()
        return pd.DatetimeIndex(periods.dropna().to_numpy())

    def run(self, features: pd.DataFrame) -> list[Selection]:
        selections = []
        for date in self.rebalance_dates(features):
            snapshot = features.xs(date, level="date", drop_level=False)
            eligible = snapshot[snapshot["eligible"]]
            symbols = self.rule(eligible) if not eligible.empty else []
            selections.append(Selection(date, symbols, len(eligible)))
        return selections

    def to_frame(self, features: pd.DataFrame) -> pd.DataFrame:
        """Selections as a tidy frame: one row per (rebalance date, symbol)."""
        rows = [
            {"date": s.date, "symbol": symbol, "rank": rank, "candidates": s.candidates}
            for s in self.run(features)
            for rank, symbol in enumerate(s.symbols)
        ]
        return pd.DataFrame(rows)


def pool_at(selections: list[Selection], when: pd.Timestamp) -> list[str]:
    """The pool in force at ``when``: the most recent selection at or before it."""
    active: list[str] = []
    for selection in selections:
        if selection.date <= when:
            active = selection.symbols
        else:
            break
    return active


def turnover(selections: list[Selection]) -> pd.Series:
    """Fraction of the pool replaced at each rebalance.

    High turnover means the rule is chasing noise, and every replacement pays the
    round-trip cost measured in the backtest cost sweep.
    """
    values = {}
    previous: set[str] = set()
    for selection in selections:
        current = set(selection.symbols)
        # Share of the new pool that was not in the old one: a fully replaced pool
        # is 100% turnover, not 50% as a union-denominator ratio would report.
        values[selection.date] = len(current - previous) / len(current) if current else 0.0
        previous = current
    return pd.Series(values, name="turnover")
