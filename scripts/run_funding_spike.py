#!/usr/bin/env python3
"""Feasibility probe: can the extreme-negative-funding spike be traded ex-ante?

The cross-sectional momentum baseline turned out to earn most of its return from a
handful of extreme negative funding settlements (short squeezes) that it was
*accidentally* long. This asks whether that exposure is a strategy on purpose:
detect the spike from data available at the time, enter the next bar, and see what
survives.

Two parts:

1. **Event study.** For every symbol-day whose funding falls below a threshold,
   look at the *next* day's price move and funding — the returns a position opened
   after the event would actually capture. No look-ahead: the trigger uses only the
   settlement already observed.
2. **Ex-ante backtest.** A long-only strategy that fires on the prior bar's funding,
   run through the full engine and cost sweep.

Verdict is recorded in DOCS.md. Usage:
    python scripts/run_funding_spike.py --root <store>
"""

from __future__ import annotations

import argparse
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from candlepilot.backtest import COST_SCENARIOS, PortfolioBacktest, build_bars
from candlepilot.backtest.engine import Intent
from candlepilot.data.store import ParquetStore
from candlepilot.evaluate import deflated_sharpe, probabilistic_sharpe, sharpe_ratio
from candlepilot.evaluate.significance import minimum_track_record_length

warnings.filterwarnings("ignore")

EVENT_THRESHOLD = -0.01  # daily funding <= -1% (longs are paid heavily)


class FundingSpikeLong:
    """Long a symbol the bar after its funding falls below ``thr``. Ex-ante by design.

    The trigger reads only ``funding_rate`` on the last closed bar, so nothing about
    the outcome is visible when the position is opened.
    """

    name = "funding_spike"

    def __init__(self, thr: float = EVENT_THRESHOLD, hold: int = 2,
                 stop_atr: float = 3.0, atr_window: int = 14):
        self.thr, self.hold, self.stop_atr, self.aw = thr, hold, stop_atr, atr_window

    def on_bar(self, ctx):
        if ctx.position is not None:
            if ctx.i - ctx.position.entry_index >= self.hold:
                return Intent("exit")
            return None
        if ctx.i < self.aw + 1:
            return None
        history = ctx.history
        if float(history["funding_rate"].iloc[-1]) > self.thr:
            return None
        recent = history.iloc[-(self.aw + 1):]
        high, low, close = (recent[c].to_numpy() for c in ("high", "low", "close"))
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
        atr = tr.mean()
        if not np.isfinite(atr) or atr <= 0:
            return None
        return Intent("long", stop_price=float(close[-1]) - self.stop_atr * atr)


def event_study(store: ParquetStore, symbols: list[str], start: str, end: str) -> None:
    recs = []
    for s in symbols:
        k = store.load_klines(s, "1d", start=start, end=end)
        if len(k) < 40:
            continue
        f = store.load_funding(s)
        if f.empty:
            continue
        daily = f["last_funding_rate"].resample("1D").sum().reindex(k.index).fillna(0.0)
        df = pd.DataFrame({"qv": k["quote_volume"], "funding": daily})
        df["ret_next"] = k["close"].shift(-1) / k["close"] - 1
        df["funding_next"] = daily.shift(-1)
        recs.append(df)
    panel = pd.concat(recs).dropna(subset=["ret_next"])

    event = panel[panel["funding"] <= EVENT_THRESHOLD].copy()
    event["net"] = event["ret_next"] - event["funding_next"]  # price + funding collected
    net = event["net"]
    best5 = int(len(net) * 0.05)
    print(f"event study: funding <= {EVENT_THRESHOLD:.0%}/day, {len(net)} events, "
          f"{event.index.size} symbol-days scanned")
    print(f"  long net: mean {net.mean():+.4f}  median {net.median():+.4f}  "
          f"win {(net > 0).mean():.1%}")
    print(f"  drop best 5% of events -> mean {net.sort_values().head(len(net)-best5).mean():+.4f}")
    print(f"  event-day median volume ${event['qv'].median():,.0f}  "
          f"(>$10M: {(event['qv'] > 1e7).mean():.0%})")
    print()


def backtest(store: ParquetStore, symbols: list[str], start: str, end: str) -> None:
    bars = {}
    for s in symbols:
        try:
            b = build_bars(store, s, interval="1d", start=start, end=end)
            if len(b) > 40 and (b["funding_rate"] <= EVENT_THRESHOLD).any():
                bars[s] = b
        except Exception:
            continue
    dates = pd.date_range(start, end, freq="W-MON", tz="UTC")
    pool = pd.DataFrame([{"date": d, "symbol": s} for d in dates for s in bars])
    print(f"ex-ante backtest: {len(bars)} symbols with events\n")

    rows, bar_sh, curves = [], {sc: [] for sc in COST_SCENARIOS}, {}
    for scenario, costs in COST_SCENARIOS.items():
        r = PortfolioBacktest(bars, pool, cost_model=costs, max_positions=10,
                              initial_equity=10000.0).run(lambda: FundingSpikeLong())
        ret = r.equity_curve.pct_change().dropna()
        bar_sh[scenario].append(sharpe_ratio(ret))
        curves[scenario] = ret
        t = r.trades_frame
        rows.append({"scenario": scenario,
                     "total_ret": r.equity_curve.iloc[-1] / 10000 - 1,
                     "sharpe_ann": sharpe_ratio(ret, periods_per_year=365),
                     "maxdd": (r.equity_curve / r.equity_curve.cummax() - 1).min(),
                     "win": float((t["net_pnl"] > 0).mean()) if len(t) else np.nan,
                     "trades": len(r.trades),
                     "psr": probabilistic_sharpe(ret),
                     "mtrl_days": minimum_track_record_length(ret)})
    tab = pd.DataFrame(rows)
    tab["dsr"] = [deflated_sharpe(curves[r.scenario],
                  trial_sharpes=np.array(bar_sh[r.scenario])) for r in tab.itertuples()]
    pd.set_option("display.width", 200)
    print(tab.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print(f"\n(data spans {len(curves['base'])} days; compare against mtrl_days)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", default="2023-01")
    parser.add_argument("--end", default="2026-06")
    args = parser.parse_args()
    store = ParquetStore(args.root)
    symbols = sorted(p.name for p in (args.root / "klines" / "1d").iterdir() if p.is_dir())
    event_study(store, symbols, args.start, args.end)
    backtest(store, symbols, args.start, args.end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
