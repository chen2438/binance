"""Shared execution semantics for single-symbol and portfolio backtests.

Both engines route every fill through ``SymbolExecutor`` so they cannot drift apart.
If the portfolio backtest resolved exits even slightly differently from the
single-symbol one, results from the two would stop being comparable — and the
difference would look like a strategy effect rather than an engine artefact.

The ordering rules implemented here are documented in ``engine``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import CostModel
from .position import DEFAULT_MMR, Position, size_for_risk


@dataclass
class Bar:
    """The per-bar values execution needs, decoupled from any DataFrame layout."""

    time: object
    open: float
    high: float
    low: float
    close: float
    mark_low: float
    mark_high: float
    mark_close: float
    funding_rate: float
    tradeable: bool


class SymbolExecutor:
    """Opens, resolves and closes positions for one symbol."""

    def __init__(
        self,
        *,
        cost_model: CostModel,
        risk_fraction: float,
        max_leverage: float,
        mmr: float = DEFAULT_MMR,
    ):
        self.costs = cost_model
        self.risk_fraction = risk_fraction
        self.max_leverage = max_leverage
        self.mmr = mmr

    def open_position(
        self,
        *,
        side: int,
        bar: Bar,
        index: int,
        equity: float,
        stop_price: float,
        target_price: float | None = None,
        risk_fraction: float | None = None,
        tags: dict | None = None,
    ) -> Position | None:
        price = self.costs.fill_price(bar.open, side, opening=True)
        qty, margin = size_for_risk(
            equity,
            price,
            stop_price,
            risk_fraction=risk_fraction if risk_fraction is not None else self.risk_fraction,
            max_leverage=self.max_leverage,
            mmr=self.mmr,
        )
        if qty <= 0:
            return None

        position = Position(
            side=side,
            entry_price=price,
            qty=qty,
            margin=margin,
            entry_index=index,
            entry_time=bar.time,
            stop_price=stop_price,
            target_price=target_price,
            mmr=self.mmr,
            tags=dict(tags or {}),
        )
        position.fees_paid += self.costs.fee(qty * price)
        return position

    def settle_funding(self, position: Position, bar: Bar) -> None:
        if bar.funding_rate:
            position.funding_paid += (
                position.side * position.qty * bar.mark_close * bar.funding_rate
            )

    def resolve_exit(self, position: Position, bar: Bar) -> tuple[float, str] | None:
        """Which level this bar triggers, assuming the worst plausible path."""
        side = position.side
        liq = position.liquidation_price()

        def fill_for(level: float) -> float:
            return min(level, bar.open) if side > 0 else max(level, bar.open)

        adverse: list[tuple[float, str]] = []

        if position.stop_price is not None:
            stop = position.stop_price
            if (bar.low <= stop) if side > 0 else (bar.high >= stop):
                adverse.append((stop, "stop"))

        liquidated = bar.tradeable and position.is_liquidated(bar.mark_low, bar.mark_high)
        if liquidated:
            adverse.append((liq, "liquidation"))

        if adverse:
            adverse.sort(key=lambda item: -item[0] if side > 0 else item[0])
            level, reason = adverse[0]
            fill = fill_for(level)
            if liquidated and ((fill < liq) if side > 0 else (fill > liq)):
                return liq, "liquidation"
            return fill, reason

        if position.target_price is not None:
            target = position.target_price
            if (bar.high >= target) if side > 0 else (bar.low <= target):
                return target, "target"

        return None

    def close_position(
        self,
        position: Position,
        price: float,
        bar: Bar,
        index: int,
        reason: str,
        *,
        symbol: str = "",
    ) -> tuple[dict, float]:
        """Close and return ``(trade_fields, net_pnl)``."""
        if reason == "liquidation":
            # Forced market exit; the exchange keeps the remaining margin, so the
            # loss is floored at what was posted.
            fill = price
        else:
            fill = self.costs.fill_price(price, position.side, opening=False)

        gross = position.side * position.qty * (fill - position.entry_price)
        fees = position.fees_paid + self.costs.fee(position.qty * fill)
        net = gross - fees - position.funding_paid
        if reason == "liquidation":
            net = max(net, -position.margin)

        fields = {
            "symbol": symbol,
            "side": position.side,
            "entry_time": position.entry_time,
            "exit_time": bar.time,
            "entry_price": position.entry_price,
            "exit_price": fill,
            "qty": position.qty,
            "margin": position.margin,
            "gross_pnl": gross,
            "fees": fees,
            "funding": position.funding_paid,
            "net_pnl": net,
            "exit_reason": reason,
            "bars_held": index - position.entry_index,
        }
        return fields, net
