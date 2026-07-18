#!/usr/bin/env python3
"""Measure the three classical hypotheses at fixed conventional parameters.

Not a search. Each hypothesis runs at one textbook parameterisation chosen in
advance, across a screened universe, under the full cost sweep. The point is to
learn which directions cannot clear costs before spending a search budget on them.

Usage:
    python scripts/run_baselines.py --root <store> [--interval 1d] [--symbols ...]
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from candlepilot.backtest import PortfolioBacktest, build_bars
from candlepilot.backtest.costs import COST_SCENARIOS
from candlepilot.backtest.metrics import BARS_PER_YEAR
from candlepilot.data.store import ParquetStore
from candlepilot.evaluate import deflated_sharpe, probabilistic_sharpe, sharpe_ratio
from candlepilot.screen import Screener, build_panel, compute_features, top_n
from candlepilot.strategies import BASELINES

warnings.filterwarnings("ignore")

# One fixed parameterisation per hypothesis, chosen before seeing any result.
BASELINE_PARAMS = {
    "momentum": {"lookback": 20, "hold": 5},
    "reversion": {"lookback": 5, "hold": 5},
    "funding_carry": {"lookback": 7, "hold": 7},
}


def bars_per_year(interval: str) -> int:
    return {"1d": 365, "1h": 365 * 24, "1m": BARS_PER_YEAR}.get(interval, 365)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--start", default="2023-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--max-positions", type=int, default=5)
    args = parser.parse_args()

    store = ParquetStore(args.root)
    symbols = args.symbols or sorted(
        p.name for p in (args.root / "klines" / args.interval).iterdir() if p.is_dir()
    )

    # Point-in-time pool from the daily panel, so the universe is screened rather
    # than hand-picked.
    panel = build_panel(store, symbols, interval="1d", start=args.start, end=args.end)
    features = compute_features(panel, window=30, min_history=30)
    pool = Screener(
        top_n("liquidity", n=args.top, filters={"liquidity": (">=", 1e6)}),
        rebalance="W-MON",
    ).to_frame(features)
    selected = sorted(set(pool["symbol"]))
    print(f"universe: {len(symbols)} ingested -> {len(selected)} ever selected")

    bars: dict[str, pd.DataFrame] = {}
    for symbol in selected:
        try:
            frame = build_bars(store, symbol, interval=args.interval,
                               start=args.start, end=args.end)
            if len(frame) > 60:
                bars[symbol] = frame
        except Exception as error:  # missing mark price, etc.
            print(f"  skip {symbol}: {str(error)[:60]}")
    print(f"loaded bars for {len(bars)} symbols\n")

    periods = bars_per_year(args.interval)
    rows = []
    # Bar-frequency Sharpes of every hypothesis, per scenario. Deflation needs the
    # spread across the trials actually run, at the same frequency the test uses —
    # feeding it an annualised-scale variance sets the luck benchmark sqrt(periods)
    # too high and fails every strategy automatically.
    bar_sharpes: dict[str, list[float]] = {s: [] for s in COST_SCENARIOS}
    curves: dict[tuple[str, str], pd.Series] = {}

    for name, factory in BASELINES.items():
        params = BASELINE_PARAMS[name]
        for scenario, costs in COST_SCENARIOS.items():
            result = PortfolioBacktest(
                bars,
                pool,
                cost_model=costs,
                max_positions=args.max_positions,
                initial_equity=10_000.0,
            ).run(lambda f=factory, p=params: f(**p))

            returns = result.equity_curve.pct_change().dropna()
            bar_sharpes[scenario].append(sharpe_ratio(returns))
            curves[(name, scenario)] = returns
            total = result.equity_curve.iloc[-1] / 10_000.0 - 1
            drawdown = (result.equity_curve / result.equity_curve.cummax() - 1).min()
            trades = result.trades_frame

            rows.append(
                {
                    "hypothesis": name,
                    "scenario": scenario,
                    "round_trip": costs.round_trip,
                    "total_return": total,
                    "max_dd": drawdown,
                    "sharpe_ann": sharpe_ratio(returns, periods_per_year=periods),
                    "trades": len(result.trades),
                    "win_rate": (
                        float((trades["net_pnl"] > 0).mean()) if len(trades) else np.nan
                    ),
                    "fees": float(trades["fees"].sum()) if len(trades) else 0.0,
                    "funding": float(trades["funding"].sum()) if len(trades) else 0.0,
                    "liquidations": result.liquidations,
                    "delisted": result.delisted_exits,
                    "psr": probabilistic_sharpe(returns),
                }
            )

    table = pd.DataFrame(rows)
    # Deflate against the three hypotheses actually tried, using their observed
    # bar-frequency spread. Three trials is a small search budget, so the penalty is
    # modest by design -- that is the whole point of fixing parameters in advance.
    table["dsr"] = [
        deflated_sharpe(
            curves[(row.hypothesis, row.scenario)],
            trial_sharpes=np.array(bar_sharpes[row.scenario]),
        )
        for row in table.itertuples()
    ]
    pd.set_option("display.width", 220)
    print(
        table.to_string(
            index=False,
            float_format=lambda v: f"{v:,.4f}",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
