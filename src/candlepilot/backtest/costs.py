"""Trading cost model.

Cost is treated as a **swept parameter, not a constant**. A single cost assumption
makes a backtest nearly uninformative for intraday work: at Binance VIP0 taker rates
a round trip already costs ~0.10% before slippage, so a strategy trading three times
a day burns ~0.4%/day. Any edge that survives the optimistic scenario but dies in the
conservative one was never an edge; running the sweep is what tells you which it is.
"""

from __future__ import annotations

from dataclasses import dataclass

# Binance USDT-M futures VIP0, as of 2026-07. Maker/taker are quoted per side.
VIP0_MAKER_FEE = 0.0002
VIP0_TAKER_FEE = 0.0005


@dataclass(frozen=True)
class CostModel:
    """Per-side fees and slippage, expressed as fractions of notional."""

    taker_fee: float = VIP0_TAKER_FEE
    maker_fee: float = VIP0_MAKER_FEE
    slippage: float = 0.0002
    name: str = "base"

    @property
    def round_trip(self) -> float:
        """Total taker round-trip cost, the number that actually constrains design."""
        return 2 * (self.taker_fee + self.slippage)

    def fill_price(self, price: float, side: int, *, opening: bool) -> float:
        """Apply slippage in the adverse direction.

        Opening a long and closing a short both buy, so both slip up; the other two
        sell and slip down. Slippage never helps.
        """
        buying = (side > 0) == opening
        return price * (1 + self.slippage) if buying else price * (1 - self.slippage)

    def fee(self, notional: float, *, maker: bool = False) -> float:
        return abs(notional) * (self.maker_fee if maker else self.taker_fee)


# Slippage tiers keyed by a symbol's median 1m quote volume (USDT). Thin symbols pay
# more for the same clip, so a flat assumption flatters exactly the illiquid alts
# where a screening rule is most likely to fire.
_LIQUIDITY_TIERS = (
    (1_000_000, 0.0001),  # majors
    (100_000, 0.0002),
    (10_000, 0.0005),
    (0, 0.0015),  # very thin
)


def slippage_for_liquidity(median_quote_volume: float) -> float:
    """Pick a slippage assumption from a symbol's typical per-bar turnover."""
    for threshold, slippage in _LIQUIDITY_TIERS:
        if median_quote_volume >= threshold:
            return slippage
    return _LIQUIDITY_TIERS[-1][1]


COST_SCENARIOS: dict[str, CostModel] = {
    # Fees only. Not realistic; it is the upper bound a strategy can never beat.
    "optimistic": CostModel(slippage=0.0, name="optimistic"),
    "base": CostModel(slippage=0.0002, name="base"),
    "conservative": CostModel(slippage=0.0005, name="conservative"),
    # Thin books or fast markets, where intraday signals tend to cluster.
    "stress": CostModel(slippage=0.0010, name="stress"),
}
