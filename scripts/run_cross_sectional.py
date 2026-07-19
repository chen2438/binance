#!/usr/bin/env python3
"""Measure cross-sectional versions of the same three hypotheses.

Same fixed conventional parameters as the time-series baselines, same universe,
same cost sweep — the only change is that the signal ranks symbols against each
other and pairs a long leg against a short leg. That pairing is the point: it
cancels most of the shared market direction that every time-series position
carries, which in crypto perps is usually larger than the signal itself.

Usage:
    python scripts/run_cross_sectional.py --root <store>
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from candlepilot.backtest import PortfolioBacktest, build_bars
from candlepilot.backtest.costs import COST_SCENARIOS
from candlepilot.data.store import ParquetStore
from candlepilot.evaluate import deflated_sharpe, probabilistic_sharpe, sharpe_ratio
from candlepilot.screen import build_panel, compute_features, long_short_pool
from candlepilot.strategies import FollowPoolSide

warnings.filterwarnings("ignore")

# Column, direction, and the hypothesis each encodes. `reverse` flips which leg is
# long, turning a momentum ranking into a reversal one without changing the feature.
HYPOTHESES = {
    "xs_momentum": {"column": "momentum", "reverse": False},
    "xs_reversion": {"column": "momentum", "reverse": True},
    # Long the most negative funding (shorts pay longs), short the most positive.
    "xs_funding": {"column": "funding_carry", "reverse": True},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", default="2023-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--legs", type=int, default=5, help="names per leg")
    parser.add_argument("--min-liquidity", type=float, default=1e6)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--symbols", nargs="+")
    args = parser.parse_args()

    store = ParquetStore(args.root)
    symbols = args.symbols or sorted(
        p.name for p in (args.root / "klines" / "1d").iterdir() if p.is_dir()
    )

    panel = build_panel(store, symbols, interval="1d", start=args.start, end=args.end)
    features = compute_features(panel, window=30, min_history=30)
    print(f"panel: {panel.index.get_level_values('symbol').nunique()} symbols")

    pools = {
        name: long_short_pool(
            features,
            spec["column"],
            n=args.legs,
            rebalance="W-MON",
            filters={"liquidity": (">=", args.min_liquidity)},
            reverse=spec["reverse"],
        )
        for name, spec in HYPOTHESES.items()
    }

    needed = sorted({s for pool in pools.values() for s in pool["symbol"]})
    bars: dict[str, pd.DataFrame] = {}
    for symbol in needed:
        try:
            frame = build_bars(store, symbol, interval="1d",
                               start=args.start, end=args.end)
            if len(frame) > 60:
                bars[symbol] = frame
        except Exception:
            continue
    print(f"loaded bars for {len(bars)} symbols\n")

    rows = []
    bar_sharpes: dict[str, list[float]] = {s: [] for s in COST_SCENARIOS}
    curves: dict[tuple[str, str], pd.Series] = {}

    for name, pool in pools.items():
        for scenario, costs in COST_SCENARIOS.items():
            result = PortfolioBacktest(
                bars, pool, cost_model=costs,
                max_positions=args.max_positions, initial_equity=10_000.0,
            ).run(lambda: FollowPoolSide())

            returns = result.equity_curve.pct_change().dropna()
            bar_sharpes[scenario].append(sharpe_ratio(returns))
            curves[(name, scenario)] = returns
            trades = result.trades_frame

            rows.append({
                "hypothesis": name,
                "scenario": scenario,
                "total_return": result.equity_curve.iloc[-1] / 10_000.0 - 1,
                "max_dd": (result.equity_curve / result.equity_curve.cummax() - 1).min(),
                "sharpe_ann": sharpe_ratio(returns, periods_per_year=365),
                "trades": len(result.trades),
                "longs": int((trades["side"] == 1).sum()) if len(trades) else 0,
                "shorts": int((trades["side"] == -1).sum()) if len(trades) else 0,
                "fees": float(trades["fees"].sum()) if len(trades) else 0.0,
                "funding": float(trades["funding"].sum()) if len(trades) else 0.0,
                "liquidations": result.liquidations,
                "psr": probabilistic_sharpe(returns),
            })

    table = pd.DataFrame(rows)
    table["dsr"] = [
        deflated_sharpe(curves[(r.hypothesis, r.scenario)],
                        trial_sharpes=np.array(bar_sharpes[r.scenario]))
        for r in table.itertuples()
    ]
    pd.set_option("display.width", 220)
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
