"""Engine ordering rules: no look-ahead, adverse intrabar path, mark-price liquidation."""

from __future__ import annotations

import pandas as pd
import pytest

from candlepilot.backtest.costs import CostModel
from candlepilot.backtest.engine import Backtest, Intent


def make_frame(rows: list[dict]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(rows), freq="1min", tz="UTC")
    frame = pd.DataFrame(rows, index=index)
    # Mark price defaults to the trade price unless a row overrides it.
    for column in ("mark_open", "mark_high", "mark_low", "mark_close"):
        if column not in frame.columns:
            frame[column] = frame[column.removeprefix("mark_")]
    for column, default in (
        ("volume", 1.0),
        ("quote_volume", 1e6),
        ("funding_rate", 0.0),
        ("tradeable", True),
    ):
        if column not in frame.columns:
            frame[column] = default
    return frame


def flat(n: int, price: float = 100.0, **kw) -> list[dict]:
    row = {"open": price, "high": price, "low": price, "close": price}
    row.update(kw)
    return [dict(row) for _ in range(n)]


class OnceStrategy:
    """Emit one intent at a given bar, then nothing."""

    def __init__(self, at: int, intent: Intent):
        self.at, self.intent = at, intent

    def on_bar(self, ctx):
        return self.intent if ctx.i == self.at else None


NO_COST = CostModel(taker_fee=0.0, maker_fee=0.0, slippage=0.0)


def test_entry_fills_at_next_bar_open_not_signal_bar() -> None:
    """Signal on bar 0 must fill at bar 1's open, never bar 0's."""
    rows = flat(4)
    rows[1].update(open=101.0, high=101.0, low=101.0, close=101.0)
    frame = make_frame(rows)

    result = Backtest(frame, cost_model=NO_COST).run(
        OnceStrategy(0, Intent("long", stop_price=95.0))
    )
    result_trades = Backtest(frame, cost_model=NO_COST)
    assert result.trades == []  # still open at the end

    # Re-run with an exit to inspect the entry price.
    class EnterThenExit:
        def on_bar(self, ctx):
            if ctx.i == 0:
                return Intent("long", stop_price=95.0)
            if ctx.i == 2:
                return Intent("exit")
            return None

    closed = result_trades.run(EnterThenExit())
    assert len(closed.trades) == 1
    assert closed.trades[0].entry_price == pytest.approx(101.0)


def test_stop_wins_over_target_in_the_same_bar() -> None:
    """Unknown intrabar path must resolve against the position."""
    rows = flat(4)
    rows[2].update(open=100.0, high=110.0, low=90.0, close=100.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=95.0, target_price=105.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop"
    assert result.trades[0].net_pnl < 0


def test_nearer_adverse_level_fires_first() -> None:
    """A stop inside the liquidation price must trigger before liquidation."""
    rows = flat(4)
    rows[2].update(open=100.0, high=100.0, low=80.0, close=85.0)
    for column in ("mark_low",):
        rows[2][column] = 80.0
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=98.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    assert result.trades[0].exit_reason == "stop"
    assert result.liquidations == 0


def test_liquidation_uses_mark_price_not_trade_price() -> None:
    """The 2020-03-13 case: trade price wicks far below mark, mark never liquidates."""
    rows = flat(4)
    # Trade price craters 8%; mark price only dips 1%.
    rows[2].update(open=100.0, high=100.0, low=92.0, close=99.0)
    rows[2].update(mark_open=100.0, mark_high=100.0, mark_low=99.0, mark_close=99.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=50.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    assert result.liquidations == 0, "mark price never reached liquidation"


def test_stop_protects_against_liquidation_on_an_ordinary_drop() -> None:
    """Correct sizing keeps the stop nearer than liquidation, so the stop wins."""
    rows = flat(4)
    rows[2].update(open=100.0, high=100.0, low=10.0, close=10.0)
    rows[2].update(mark_open=100.0, mark_high=100.0, mark_low=10.0, mark_close=10.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=50.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    assert result.trades[0].exit_reason == "stop"
    assert result.liquidations == 0


def test_gap_through_the_stop_fills_at_the_open_not_the_stop() -> None:
    """A stop is a trigger, not a guaranteed price."""
    rows = flat(4)
    rows[2].update(open=60.0, high=60.0, low=55.0, close=58.0)
    rows[2].update(mark_open=60.0, mark_high=60.0, mark_low=55.0, mark_close=58.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=70.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(60.0), "filled at the gapped open"


def test_gap_beyond_liquidation_is_reclassified_as_liquidation() -> None:
    """The realistic route to liquidation: a gap straight through the stop."""
    rows = flat(4)
    rows[2].update(open=20.0, high=20.0, low=20.0, close=20.0)
    rows[2].update(mark_open=20.0, mark_high=20.0, mark_low=20.0, mark_close=20.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=50.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    trade = result.trades[0]
    assert trade.exit_reason == "liquidation"
    assert result.liquidations == 1


def test_liquidation_loss_is_floored_at_posted_margin() -> None:
    rows = flat(4)
    rows[2].update(open=5.0, high=5.0, low=5.0, close=5.0)
    rows[2].update(mark_open=5.0, mark_high=5.0, mark_low=5.0, mark_close=5.0)
    frame = make_frame(rows)

    strategy = OnceStrategy(0, Intent("long", stop_price=50.0))
    result = Backtest(frame, cost_model=NO_COST).run(strategy)

    trade = result.trades[0]
    assert trade.exit_reason == "liquidation"
    # Liquidation fires when equity reaches maintenance margin, so the loss lands
    # just short of the posted margin and never beyond it.
    assert trade.net_pnl >= -trade.margin - 1e-9
    assert trade.net_pnl < -0.95 * trade.margin


def test_sizing_keeps_liquidation_beyond_the_stop() -> None:
    """The invariant the whole liquidation design rests on."""
    from candlepilot.backtest.position import Position, size_for_risk

    for stop_pct in (0.002, 0.01, 0.03, 0.08, 0.20):
        entry = 100.0
        stop = entry * (1 - stop_pct)
        qty, margin = size_for_risk(
            10_000.0, entry, stop, risk_fraction=0.01, max_leverage=20.0
        )
        position = Position(
            side=1, entry_price=entry, qty=qty, margin=margin, entry_index=0,
            entry_time=None,
        )
        assert position.liquidation_price() < stop, f"stop_pct={stop_pct}"
        assert position.leverage <= 20.0 + 1e-9


def test_untradeable_bar_blocks_entry() -> None:
    """No usable mark price means the liquidation check is blind; refuse to enter."""
    rows = flat(4)
    frame = make_frame(rows)
    frame.loc[frame.index[1], "tradeable"] = False

    result = Backtest(frame, cost_model=NO_COST).run(
        OnceStrategy(0, Intent("long", stop_price=95.0))
    )
    assert result.trades == []
    assert result.skipped_untradeable == 1


def test_funding_is_charged_to_an_open_long() -> None:
    rows = flat(5)
    frame = make_frame(rows)
    frame.loc[frame.index[2], "funding_rate"] = 0.0001

    class EnterThenExit:
        def on_bar(self, ctx):
            if ctx.i == 0:
                return Intent("long", stop_price=95.0)
            if ctx.i == 3:
                return Intent("exit")
            return None

    result = Backtest(frame, cost_model=NO_COST).run(EnterThenExit())
    trade = result.trades[0]
    assert trade.funding > 0
    assert trade.net_pnl == pytest.approx(-trade.funding)


def test_funding_is_received_by_a_short_when_rate_is_positive() -> None:
    rows = flat(5)
    frame = make_frame(rows)
    frame.loc[frame.index[2], "funding_rate"] = 0.0001

    class EnterThenExit:
        def on_bar(self, ctx):
            if ctx.i == 0:
                return Intent("short", stop_price=105.0)
            if ctx.i == 3:
                return Intent("exit")
            return None

    result = Backtest(frame, cost_model=NO_COST).run(EnterThenExit())
    assert result.trades[0].funding < 0


def test_costs_are_charged_on_both_sides() -> None:
    rows = flat(4)
    frame = make_frame(rows)
    costs = CostModel(taker_fee=0.0005, maker_fee=0.0002, slippage=0.0)

    class EnterThenExit:
        def on_bar(self, ctx):
            if ctx.i == 0:
                return Intent("long", stop_price=95.0)
            if ctx.i == 2:
                return Intent("exit")
            return None

    result = Backtest(frame, cost_model=costs).run(EnterThenExit())
    trade = result.trades[0]
    assert trade.fees == pytest.approx(2 * trade.qty * 100.0 * 0.0005)


def test_slippage_always_hurts() -> None:
    costs = CostModel(slippage=0.001)
    assert costs.fill_price(100.0, side=1, opening=True) > 100.0  # buy to open
    assert costs.fill_price(100.0, side=1, opening=False) < 100.0  # sell to close
    assert costs.fill_price(100.0, side=-1, opening=True) < 100.0  # sell to open
    assert costs.fill_price(100.0, side=-1, opening=False) > 100.0  # buy to close


def test_strategy_cannot_see_future_bars() -> None:
    frame = make_frame(flat(10))
    seen: list[int] = []

    class Peeker:
        def on_bar(self, ctx):
            seen.append(len(ctx.history))
            assert ctx.history.index[-1] == ctx.bar.name
            return None

    Backtest(frame, cost_model=NO_COST).run(Peeker())
    assert seen == list(range(1, 11))


def test_leverage_above_cap_is_rejected() -> None:
    frame = make_frame(flat(3))
    with pytest.raises(ValueError, match="20x"):
        Backtest(frame, max_leverage=25.0)
