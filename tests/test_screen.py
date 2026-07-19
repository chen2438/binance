"""Screening tests. The point-in-time guarantee is the thing worth testing hardest."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlepilot.screen.features import compute_features
from candlepilot.screen.screener import Screener, pool_at, top_n, turnover


def make_panel(spec: dict[str, int], *, start: str = "2024-01-01") -> pd.DataFrame:
    """Build a panel where each symbol trades for a given number of days."""
    frames = []
    for symbol, days in spec.items():
        dates = pd.date_range(start, periods=days, freq="1D", tz="UTC")
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 1.0,
                    "quote_volume": 1e6,
                    "count": 100,
                    "funding_rate": 0.0001,
                }
            )
        )
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["date", "symbol"]).set_index(["date", "symbol"])


def test_feature_on_a_date_cannot_see_that_date() -> None:
    """The core guarantee, pinned to an exact value.

    Volumes are made distinct per day so the rolling median has a single correct
    answer; a median over identical values could not reveal a one-day leak.
    """
    panel = make_panel({"AAAUSDT": 40})
    dates = panel.index.get_level_values("date")
    panel["quote_volume"] = np.arange(1, 41, dtype=float)

    features = compute_features(panel, window=5, min_history=5)

    # Day 20's feature must be the median of days 15..19 -> volumes 16..20 -> 18.
    assert features.loc[(dates[20], "AAAUSDT"), "liquidity"] == pytest.approx(18.0)
    # Day 21 shifts the window by exactly one day.
    assert features.loc[(dates[21], "AAAUSDT"), "liquidity"] == pytest.approx(19.0)


def test_shift_does_not_leak_across_symbols() -> None:
    """Each symbol is shifted in isolation; B must not inherit A's last row."""
    panel = make_panel({"AAAUSDT": 40, "BBBUSDT": 40})
    dates = panel.index.get_level_values("date").unique()
    # A carries huge volumes, B carries tiny ones.
    panel.loc[(slice(None), "AAAUSDT"), "quote_volume"] = 1e12
    panel.loc[(slice(None), "BBBUSDT"), "quote_volume"] = 1.0

    features = compute_features(panel, window=5, min_history=5)
    assert features.loc[(dates[20], "BBBUSDT"), "liquidity"] == pytest.approx(1.0)
    assert features.loc[(dates[20], "AAAUSDT"), "liquidity"] == pytest.approx(1e12)


def test_first_feature_appears_exactly_one_day_after_the_window_fills() -> None:
    """Off-by-one in the shift would show up here."""
    panel = make_panel({"AAAUSDT": 20})
    dates = panel.index.get_level_values("date")
    features = compute_features(panel, window=5, min_history=1)

    # window=5 needs days 0..4, so the first non-null rolling value sits on day 4;
    # after the shift it must first appear on day 5.
    assert pd.isna(features.loc[(dates[4], "AAAUSDT"), "liquidity"])
    assert not pd.isna(features.loc[(dates[5], "AAAUSDT"), "liquidity"])


def test_first_bar_of_each_symbol_has_no_feature() -> None:
    panel = make_panel({"AAAUSDT": 40, "BBBUSDT": 40})
    features = compute_features(panel, window=5, min_history=5)
    dates = panel.index.get_level_values("date").unique()
    for symbol in ("AAAUSDT", "BBBUSDT"):
        assert not features.loc[(dates[0], symbol), "eligible"]


def test_eligibility_requires_minimum_history() -> None:
    """A freshly listed symbol must not be rankable on three days of data."""
    panel = make_panel({"OLDUSDT": 60, "NEWUSDT": 60})
    # NEWUSDT lists 50 days late.
    new_rows = panel.xs("NEWUSDT", level="symbol", drop_level=False)
    panel = panel.drop(new_rows.index[:50])

    features = compute_features(panel, window=10, min_history=30)
    last_date = panel.index.get_level_values("date").max()
    assert features.loc[(last_date, "OLDUSDT"), "eligible"]
    assert not features.loc[(last_date, "NEWUSDT"), "eligible"]


def test_delisted_symbol_is_absent_after_its_last_day() -> None:
    panel = make_panel({"LIVEUSDT": 60, "DEADUSDT": 40})
    features = compute_features(panel, window=10, min_history=15)
    dates = panel.index.get_level_values("date").unique()

    screener = Screener(top_n("liquidity", n=10), rebalance="W-MON")
    frame = screener.to_frame(features)

    late = frame[frame["date"] > dates[45]]
    assert "DEADUSDT" not in set(late["symbol"]), "delisted symbol selected after delisting"


def test_delisted_symbol_is_selectable_while_it_was_live() -> None:
    """Survivorship defence: it must appear in pools chosen before it delisted."""
    panel = make_panel({"LIVEUSDT": 60, "DEADUSDT": 40})
    features = compute_features(panel, window=10, min_history=15)

    screener = Screener(top_n("liquidity", n=10), rebalance="W-MON")
    frame = screener.to_frame(features)

    assert "DEADUSDT" in set(frame["symbol"]), "delisted symbol never selectable at all"


def test_top_n_ranks_descending_by_default() -> None:
    snapshot = pd.DataFrame(
        {"liquidity": [1.0, 3.0, 2.0], "eligible": True},
        index=pd.MultiIndex.from_product(
            [[pd.Timestamp("2024-01-01", tz="UTC")], ["A", "B", "C"]],
            names=["date", "symbol"],
        ),
    )
    assert top_n("liquidity", n=2)(snapshot) == ["B", "C"]
    assert top_n("liquidity", n=2, ascending=True)(snapshot) == ["A", "C"]


def test_top_n_applies_threshold_filters() -> None:
    snapshot = pd.DataFrame(
        {"liquidity": [1.0, 3.0, 2.0], "realized_vol": [0.1, 0.9, 0.5]},
        index=pd.MultiIndex.from_product(
            [[pd.Timestamp("2024-01-01", tz="UTC")], ["A", "B", "C"]],
            names=["date", "symbol"],
        ),
    )
    rule = top_n("liquidity", n=5, filters={"realized_vol": ("<=", 0.6)})
    assert rule(snapshot) == ["C", "A"]


def test_top_n_rejects_unknown_filter_column() -> None:
    snapshot = pd.DataFrame(
        {"liquidity": [1.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2024-01-01", tz="UTC"), "A")], names=["date", "symbol"]
        ),
    )
    with pytest.raises(KeyError, match="not a computed feature"):
        top_n("liquidity", filters={"nope": (">=", 1)})(snapshot)


def test_pool_at_returns_the_selection_in_force() -> None:
    from candlepilot.screen.screener import Selection

    selections = [
        Selection(pd.Timestamp("2024-01-01", tz="UTC"), ["A"], 5),
        Selection(pd.Timestamp("2024-01-08", tz="UTC"), ["B"], 5),
    ]
    assert pool_at(selections, pd.Timestamp("2024-01-05", tz="UTC")) == ["A"]
    assert pool_at(selections, pd.Timestamp("2024-01-08", tz="UTC")) == ["B"]
    assert pool_at(selections, pd.Timestamp("2023-12-31", tz="UTC")) == []


def test_turnover_measures_pool_replacement() -> None:
    from candlepilot.screen.screener import Selection

    selections = [
        Selection(pd.Timestamp("2024-01-01", tz="UTC"), ["A", "B"], 5),
        Selection(pd.Timestamp("2024-01-08", tz="UTC"), ["A", "B"], 5),
        Selection(pd.Timestamp("2024-01-15", tz="UTC"), ["C", "D"], 5),
    ]
    values = turnover(selections)
    assert values.iloc[1] == pytest.approx(0.0)
    assert values.iloc[2] == pytest.approx(1.0)


def test_rebalance_dates_snap_to_dates_present_in_the_panel() -> None:
    panel = make_panel({"AAAUSDT": 40})
    features = compute_features(panel, window=5, min_history=5)
    screener = Screener(top_n("liquidity"), rebalance="W-MON")
    dates = set(features.index.get_level_values("date"))
    assert set(screener.rebalance_dates(features)).issubset(dates)


def test_momentum_is_computed_from_prior_closes_only() -> None:
    dates = pd.date_range("2024-01-01", periods=30, freq="1D", tz="UTC")
    closes = np.linspace(100, 130, 30)
    panel = pd.DataFrame(
        {
            "date": dates,
            "symbol": "AAAUSDT",
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": 1.0,
            "quote_volume": 1e6,
            "count": 10,
            "funding_rate": 0.0,
        }
    ).set_index(["date", "symbol"])

    features = compute_features(panel, window=5, min_history=5)
    # Momentum carried on day 20 must equal the 5-day return ending on day 19.
    expected = closes[19] / closes[14] - 1
    assert features.loc[(dates[20], "AAAUSDT"), "momentum"] == pytest.approx(expected)


# ------------------------------------------------------------ cross-sectional


def test_long_short_pool_assigns_both_legs() -> None:
    from candlepilot.screen.cross import long_short_pool

    panel = make_panel({f"S{i}USDT": 60 for i in range(10)})
    dates = panel.index.get_level_values("date").unique()
    # Give each symbol a distinct, constant volume so the ranking is unambiguous.
    for i in range(10):
        panel.loc[(slice(None), f"S{i}USDT"), "quote_volume"] = float(i + 1) * 1e6

    features = compute_features(panel, window=10, min_history=15)
    pool = long_short_pool(features, "liquidity", n=2, rebalance="W-MON")

    assert not pool.empty
    assert set(pool["side"]) == {1, -1}
    last = pool[pool["date"] == pool["date"].max()]
    longs = set(last[last["side"] == 1]["symbol"])
    shorts = set(last[last["side"] == -1]["symbol"])
    assert longs == {"S9USDT", "S8USDT"}, "long leg must be the top of the ranking"
    assert shorts == {"S0USDT", "S1USDT"}, "short leg must be the bottom"
    assert not (longs & shorts)


def test_reverse_flips_the_legs() -> None:
    from candlepilot.screen.cross import long_short_pool

    panel = make_panel({f"S{i}USDT": 60 for i in range(10)})
    for i in range(10):
        panel.loc[(slice(None), f"S{i}USDT"), "quote_volume"] = float(i + 1) * 1e6
    features = compute_features(panel, window=10, min_history=15)

    normal = long_short_pool(features, "liquidity", n=2)
    flipped = long_short_pool(features, "liquidity", n=2, reverse=True)

    date = normal["date"].max()
    normal_longs = set(normal[(normal["date"] == date) & (normal["side"] == 1)]["symbol"])
    flipped_shorts = set(flipped[(flipped["date"] == date) & (flipped["side"] == -1)]["symbol"])
    assert normal_longs == flipped_shorts


def test_pool_is_skipped_when_too_few_names_to_rank() -> None:
    """Fewer than 2n eligible names would make the legs overlap."""
    from candlepilot.screen.cross import long_short_pool

    panel = make_panel({"AUSDT": 60, "BUSDT": 60})
    features = compute_features(panel, window=10, min_history=15)
    assert long_short_pool(features, "liquidity", n=5).empty
