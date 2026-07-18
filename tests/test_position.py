"""Liquidation math and risk-based sizing."""

from __future__ import annotations

import pytest

from candlepilot.backtest.position import DEFAULT_MMR, Position, size_for_risk


def make_position(side: int = 1, entry: float = 100.0, leverage: float = 20.0) -> Position:
    qty = 10.0
    return Position(
        side=side,
        entry_price=entry,
        qty=qty,
        margin=qty * entry / leverage,
        entry_index=0,
        entry_time=None,
    )


def test_long_liquidation_sits_just_under_five_percent_at_20x() -> None:
    """The headline number the whole liquidation design rests on."""
    position = make_position(side=1)
    liq = position.liquidation_price()
    distance = (position.entry_price - liq) / position.entry_price
    assert 0.044 < distance < 0.046


def test_short_liquidation_is_symmetric_above_entry() -> None:
    position = make_position(side=-1)
    liq = position.liquidation_price()
    assert liq > position.entry_price
    distance = (liq - position.entry_price) / position.entry_price
    assert 0.044 < distance < 0.046


def test_lower_leverage_moves_liquidation_further_away() -> None:
    near = make_position(leverage=20.0).liquidation_price()
    far = make_position(leverage=5.0).liquidation_price()
    assert far < near


def test_funding_paid_pulls_liquidation_closer() -> None:
    """Funding bleeds margin, so a carried position liquidates earlier."""
    position = make_position(side=1)
    baseline = position.liquidation_price()
    position.funding_paid = 2.0
    assert position.liquidation_price() > baseline


def test_liquidation_uses_mark_range() -> None:
    position = make_position(side=1)
    liq = position.liquidation_price()
    assert not position.is_liquidated(mark_low=liq + 0.5, mark_high=110.0)
    assert position.is_liquidated(mark_low=liq - 0.01, mark_high=110.0)


def test_size_for_risk_loses_exactly_the_risk_budget_at_stop() -> None:
    equity, entry, stop = 10_000.0, 100.0, 98.0
    qty, _ = size_for_risk(
        equity, entry, stop, risk_fraction=0.01, max_leverage=20.0
    )
    assert qty * (entry - stop) == pytest.approx(equity * 0.01)


def test_size_for_risk_caps_at_max_leverage() -> None:
    """A very tight stop must not translate into unbounded notional."""
    equity, entry, stop = 10_000.0, 100.0, 99.99
    qty, margin = size_for_risk(
        equity, entry, stop, risk_fraction=0.01, max_leverage=20.0
    )
    assert qty * entry == pytest.approx(equity * 20.0)
    assert margin == pytest.approx(equity)


def test_size_for_risk_rejects_degenerate_stop() -> None:
    assert size_for_risk(10_000.0, 100.0, 100.0, risk_fraction=0.01, max_leverage=20.0) == (
        0.0,
        0.0,
    )


def test_default_mmr_is_conservative() -> None:
    assert DEFAULT_MMR >= 0.004
