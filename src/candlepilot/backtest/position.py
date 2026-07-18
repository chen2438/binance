"""Position state and isolated-margin liquidation math.

Liquidation is deliberately modelled coarsely, for a reason grounded in the data.
Binance's real maintenance margin comes from notional-bracket tiers that change over
time and are not published historically, so a "precise" reconstruction would be
precisely wrong. A single conservative rate is honest about that.

The approximation is cheap because liquidation should never be the binding
constraint: at 20x it sits ~4.5% away, while the largest single-minute mark-price
drop measured on DOGEUSDT during the 2024-08 crash month was 3.0%. Liquidation is a
backstop that says the position was sized wrong, not a normal exit — so
``BacktestResult`` reports liquidations separately instead of burying them in the
trade list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Conservative stand-in for Binance's notional-tiered maintenance margin rate. Real
# tier 1 rates run 0.4%-0.5% for majors and higher for small caps; erring high makes
# liquidation trigger slightly early, which is the safe direction for a backtest.
DEFAULT_MMR = 0.005


@dataclass
class Position:
    """One open isolated-margin position."""

    side: int  # +1 long, -1 short
    entry_price: float
    qty: float
    margin: float
    entry_index: int
    entry_time: object
    stop_price: float | None = None
    target_price: float | None = None
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    mmr: float = DEFAULT_MMR
    tags: dict = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    @property
    def leverage(self) -> float:
        return self.notional / self.margin if self.margin else float("inf")

    def unrealized(self, price: float) -> float:
        return self.side * self.qty * (price - self.entry_price)

    def liquidation_price(self) -> float:
        """Mark price at which margin no longer covers maintenance margin.

        Solves ``margin + side*qty*(P - entry) == qty*P*mmr``. Funding already paid
        is deducted from margin, so a position bled by funding liquidates earlier —
        which is the whole point of carrying funding through the position.
        """
        equity = self.margin - self.funding_paid
        if self.side > 0:
            denominator = self.qty * (1 - self.mmr)
            return (self.qty * self.entry_price - equity) / denominator
        denominator = self.qty * (1 + self.mmr)
        return (equity + self.qty * self.entry_price) / denominator

    def is_liquidated(self, mark_low: float, mark_high: float) -> bool:
        """Liquidation is evaluated on **mark** price, as Binance does."""
        liq = self.liquidation_price()
        return mark_low <= liq if self.side > 0 else mark_high >= liq


# How far beyond the stop the liquidation price must sit. Posting only the minimum
# margin pins every position at max leverage, which fixes liquidation ~4.5% away and
# silently swallows any stop wider than that — liquidation would become the normal
# exit instead of a backstop.
DEFAULT_LIQUIDATION_BUFFER = 1.5


def margin_for_stop_buffer(
    qty: float,
    entry_price: float,
    stop_distance: float,
    *,
    mmr: float = DEFAULT_MMR,
    buffer: float = DEFAULT_LIQUIDATION_BUFFER,
) -> float:
    """Margin needed to keep liquidation at least ``buffer`` x the stop distance away.

    Derived from the liquidation equation: requiring
    ``liq <= entry - buffer*stop_distance`` for a long gives
    ``margin >= qty * (entry*mmr + (1-mmr)*buffer*stop_distance)``. The short side
    yields the same expression.
    """
    return qty * (entry_price * mmr + (1 - mmr) * buffer * stop_distance)


def size_for_risk(
    equity: float,
    entry_price: float,
    stop_price: float,
    *,
    risk_fraction: float,
    max_leverage: float,
    mmr: float = DEFAULT_MMR,
    liquidation_buffer: float = DEFAULT_LIQUIDATION_BUFFER,
) -> tuple[float, float]:
    """Size a position from risk-per-trade, then post margin that protects the stop.

    Sizing from the stop distance rather than a leverage multiple keeps
    ``max_leverage`` from becoming the de facto position size: leverage is a margin
    efficiency setting, while the stop determines the loss. Margin is then raised
    above the leverage minimum whenever the stop is wide, so the stop — not
    liquidation — is what actually closes a losing trade.

    Returns ``(qty, margin)``; ``(0, 0)`` when the stop is unusable.
    """
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0 or entry_price <= 0 or equity <= 0:
        return 0.0, 0.0

    qty = (equity * risk_fraction) / stop_distance

    # Leverage cap on notional.
    max_notional = equity * max_leverage
    if qty * entry_price > max_notional:
        qty = max_notional / entry_price

    margin = max(
        qty * entry_price / max_leverage,
        margin_for_stop_buffer(
            qty, entry_price, stop_distance, mmr=mmr, buffer=liquidation_buffer
        ),
    )

    # Margin cannot exceed equity; scale the position down rather than over-commit.
    if margin > equity:
        qty *= equity / margin
        margin = equity

    return qty, margin
