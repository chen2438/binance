"""Portfolio policy: pool exits, delisting, shared equity, capacity."""

from __future__ import annotations

import pandas as pd
import pytest

from candlepilot.backtest.costs import CostModel
from candlepilot.backtest.engine import Intent
from candlepilot.backtest.portfolio import PortfolioBacktest

NO_COST = CostModel(taker_fee=0.0, maker_fee=0.0, slippage=0.0)


def make_bars(n: int, price: float = 100.0, start: str = "2024-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1.0,
            "quote_volume": 1e6,
            "funding_rate": 0.0,
            "tradeable": True,
        },
        index=index,
    )
    for column in ("mark_open", "mark_high", "mark_low", "mark_close"):
        frame[column] = price
    return frame


def make_pool(entries: dict[str, list[str]]) -> pd.DataFrame:
    rows = [
        {"date": pd.Timestamp(date, tz="UTC"), "symbol": symbol}
        for date, symbols in entries.items()
        for symbol in symbols
    ]
    return pd.DataFrame(rows)


class EnterAt:
    """Enter long at a given bar index and then hold."""

    def __init__(self, at: int = 0, stop: float = 95.0):
        self.at, self.stop = at, stop

    def on_bar(self, ctx):
        if ctx.i == self.at and ctx.position is None:
            return Intent("long", stop_price=self.stop)
        return None


def test_symbol_outside_the_pool_cannot_be_entered() -> None:
    bars = {"AAAUSDT": make_bars(10), "BBBUSDT": make_bars(10)}
    pool = make_pool({"2024-01-01": ["AAAUSDT"]})

    result = PortfolioBacktest(bars, pool, cost_model=NO_COST).run(lambda: EnterAt(0))

    symbols = {t.symbol for t in result.trades}
    assert "BBBUSDT" not in symbols
    assert result.skipped_out_of_pool >= 1


def test_leaving_the_pool_does_not_close_an_open_position() -> None:
    """The screen must not overwrite the strategy's own exit logic."""
    bars = {"AAAUSDT": make_bars(10)}
    pool = make_pool(
        {"2024-01-01 00:00": ["AAAUSDT"], "2024-01-01 00:05": []}  # dropped mid-run
    )
    pool = pool[pool["symbol"] != ""]

    result = PortfolioBacktest(bars, pool, cost_model=NO_COST).run(lambda: EnterAt(0))

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end_of_data", "pool exit forced a close"


def test_close_on_pool_exit_is_opt_in() -> None:
    bars = {"AAAUSDT": make_bars(10)}
    rows = [{"date": pd.Timestamp("2024-01-01", tz="UTC"), "symbol": "AAAUSDT"},
            {"date": pd.Timestamp("2024-01-01 00:05", tz="UTC"), "symbol": "ZZZUSDT"}]
    pool = pd.DataFrame(rows)

    result = PortfolioBacktest(
        bars, pool, cost_model=NO_COST, close_on_pool_exit=True
    ).run(lambda: EnterAt(0))

    assert result.trades[0].exit_reason == "pool_exit"


def test_delisting_forces_a_close_and_books_the_loss() -> None:
    """A vanished symbol must not take its loss with it."""
    short = make_bars(5, price=100.0)
    short.iloc[-1, short.columns.get_loc("close")] = 90.0
    bars = {"DEADUSDT": short, "LIVEUSDT": make_bars(20)}
    pool = make_pool({"2024-01-01": ["DEADUSDT", "LIVEUSDT"]})

    result = PortfolioBacktest(
        bars, pool, cost_model=NO_COST, max_positions=5
    ).run(lambda: EnterAt(0, stop=50.0))

    dead = [t for t in result.trades if t.symbol == "DEADUSDT"]
    assert len(dead) == 1
    assert dead[0].exit_reason in ("delisted", "end_of_data")
    assert dead[0].net_pnl < 0, "delisted position booked no loss"
    assert result.delisted_exits >= 1


def test_max_positions_caps_concurrency() -> None:
    bars = {f"S{i}USDT": make_bars(10) for i in range(5)}
    pool = make_pool({"2024-01-01": list(bars)})

    result = PortfolioBacktest(
        bars, pool, cost_model=NO_COST, max_positions=2
    ).run(lambda: EnterAt(0))

    assert len(result.trades) == 2
    assert result.skipped_at_capacity >= 1


def test_equity_is_shared_not_duplicated_per_symbol() -> None:
    """Two symbols must size against one pot, not a private pot each."""
    bars = {"AAAUSDT": make_bars(10), "BBBUSDT": make_bars(10)}
    pool = make_pool({"2024-01-01": ["AAAUSDT", "BBBUSDT"]})

    both = PortfolioBacktest(
        bars, pool, cost_model=NO_COST, max_positions=2, initial_equity=10_000.0
    ).run(lambda: EnterAt(0))

    one = PortfolioBacktest(
        {"AAAUSDT": bars["AAAUSDT"]},
        make_pool({"2024-01-01": ["AAAUSDT"]}),
        cost_model=NO_COST,
        max_positions=2,
        initial_equity=10_000.0,
    ).run(lambda: EnterAt(0))

    first_of_both = next(t for t in both.trades if t.symbol == "AAAUSDT")
    only = one.trades[0]
    # The first entry sizes off the same equity either way...
    assert first_of_both.qty == pytest.approx(only.qty)
    # ...but the second position competes for the same capital.
    second = next(t for t in both.trades if t.symbol == "BBBUSDT")
    assert second.margin > 0


def test_accounting_reconciles_with_the_equity_curve() -> None:
    bars = {"AAAUSDT": make_bars(20), "BBBUSDT": make_bars(20)}
    pool = make_pool({"2024-01-01": ["AAAUSDT", "BBBUSDT"]})

    result = PortfolioBacktest(bars, pool, cost_model=NO_COST).run(lambda: EnterAt(0))

    realized = sum(t.net_pnl for t in result.trades)
    assert result.initial_equity + realized == pytest.approx(
        result.equity_curve.iloc[-1], abs=1e-6
    )


def test_accounting_reconciles_when_costs_are_charged() -> None:
    """With zero costs a mark-to-market finish looks correct even when it isn't.

    Positions still open at the end pay exit fees on settlement; if the curve keeps
    its marked-to-market last point it overstates the finish by exactly those fees.
    """
    bars = {"AAAUSDT": make_bars(20), "BBBUSDT": make_bars(20)}
    pool = make_pool({"2024-01-01": ["AAAUSDT", "BBBUSDT"]})

    result = PortfolioBacktest(
        bars, pool, cost_model=CostModel(taker_fee=0.0005, slippage=0.0002)
    ).run(lambda: EnterAt(0))

    assert any(t.exit_reason == "end_of_data" for t in result.trades)
    realized = sum(t.net_pnl for t in result.trades)
    assert result.initial_equity + realized == pytest.approx(
        result.equity_curve.iloc[-1], abs=1e-6
    )


def test_funding_accrues_per_symbol() -> None:
    bars = {"AAAUSDT": make_bars(10)}
    bars["AAAUSDT"].loc[bars["AAAUSDT"].index[3], "funding_rate"] = 0.001
    pool = make_pool({"2024-01-01": ["AAAUSDT"]})

    result = PortfolioBacktest(bars, pool, cost_model=NO_COST).run(lambda: EnterAt(0))
    assert result.trades[0].funding > 0


def test_symbols_with_different_histories_align_on_one_timeline() -> None:
    """A late-listing symbol must not shift another symbol's bars."""
    early = make_bars(20, price=100.0, start="2024-01-01 00:00")
    late = make_bars(10, price=200.0, start="2024-01-01 00:10")
    bars = {"EARLYUSDT": early, "LATEUSDT": late}
    pool = make_pool({"2024-01-01": ["EARLYUSDT", "LATEUSDT"]})

    result = PortfolioBacktest(bars, pool, cost_model=NO_COST, max_positions=5).run(
        lambda: EnterAt(0)
    )

    by_symbol = {t.symbol: t for t in result.trades}
    assert by_symbol["EARLYUSDT"].entry_price == pytest.approx(100.0)
    assert by_symbol["LATEUSDT"].entry_price == pytest.approx(200.0)
    assert len(result.equity_curve) == 20


def test_leverage_cap_is_enforced() -> None:
    bars = {"AAAUSDT": make_bars(5)}
    pool = make_pool({"2024-01-01": ["AAAUSDT"]})
    with pytest.raises(ValueError, match="20x"):
        PortfolioBacktest(bars, pool, max_leverage=25.0)
