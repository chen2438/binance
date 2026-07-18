"""Splits, significance math, and walk-forward discipline."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from candlepilot.evaluate.significance import (
    deflated_sharpe,
    expected_max_sharpe,
    minimum_track_record_length,
    probabilistic_sharpe,
    sharpe_ratio,
)
from candlepilot.evaluate.splits import walk_forward

# ------------------------------------------------------------------------ splits


def daily_index(days: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=days, freq="1D", tz="UTC")


def test_walk_forward_produces_non_overlapping_test_windows() -> None:
    splits = walk_forward(daily_index(400), train="90D", test="30D")
    for earlier, later in zip(splits, splits[1:]):
        assert earlier.test_end <= later.test_start


def test_embargo_separates_train_from_test() -> None:
    """Without a gap, the first test bar's lookback reaches into training data."""
    splits = walk_forward(daily_index(400), train="90D", test="30D", embargo="7D")
    assert splits
    for split in splits:
        assert split.embargo == pd.Timedelta("7D")
        assert split.test_start > split.train_end


def test_zero_embargo_is_the_default_and_is_explicit() -> None:
    splits = walk_forward(daily_index(200), train="60D", test="30D")
    assert all(split.embargo == pd.Timedelta(0) for split in splits)


def test_rolling_windows_have_constant_train_length() -> None:
    splits = walk_forward(daily_index(500), train="90D", test="30D", anchored=False)
    lengths = {split.train_end - split.train_start for split in splits}
    assert lengths == {pd.Timedelta("90D")}


def test_anchored_windows_grow() -> None:
    splits = walk_forward(daily_index(500), train="90D", test="30D", anchored=True)
    spans = [split.train_end - split.train_start for split in splits]
    assert spans == sorted(spans)
    assert spans[-1] > spans[0]
    assert len({split.train_start for split in splits}) == 1


def test_no_folds_when_data_is_too_short() -> None:
    assert walk_forward(daily_index(30), train="90D", test="30D") == []


def test_empty_index_yields_no_folds() -> None:
    assert walk_forward(pd.DatetimeIndex([]), train="90D", test="30D") == []


def test_negative_spans_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        walk_forward(daily_index(200), train="0D", test="30D")


# ------------------------------------------------------------------ significance


def test_sharpe_of_pure_noise_is_near_zero() -> None:
    rng = np.random.default_rng(0)
    assert abs(sharpe_ratio(rng.normal(0, 0.01, 20_000))) < 0.05


def test_sharpe_annualisation_scales_by_sqrt_periods() -> None:
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0005, 0.01, 5_000)
    raw = sharpe_ratio(returns)
    annual = sharpe_ratio(returns, periods_per_year=365)
    assert annual == pytest.approx(raw * math.sqrt(365))


def test_psr_rises_with_sample_length_for_the_same_sharpe() -> None:
    """The same edge observed longer is more believable; PSR must reflect that."""
    rng = np.random.default_rng(2)
    short = rng.normal(0.001, 0.01, 100)
    long = np.tile(short, 20)  # identical Sharpe, 20x the observations
    assert probabilistic_sharpe(long) > probabilistic_sharpe(short)


def test_psr_of_a_coin_flip_is_around_a_half() -> None:
    rng = np.random.default_rng(3)
    values = probabilistic_sharpe(rng.normal(0, 0.01, 5_000))
    assert 0.2 < values < 0.8


def test_psr_punishes_negative_skew() -> None:
    """Fat left tails make the same Sharpe less trustworthy."""
    rng = np.random.default_rng(4)
    symmetric = rng.normal(0.001, 0.01, 4_000)

    skewed = symmetric.copy()
    skewed[:40] -= 0.08  # rare large losses
    skewed = skewed - skewed.mean() + symmetric.mean()
    skewed = skewed / skewed.std() * symmetric.std()

    assert probabilistic_sharpe(skewed) < probabilistic_sharpe(symmetric)


def test_expected_max_sharpe_grows_with_the_number_of_trials() -> None:
    """The core anti-overfitting fact: more trials, higher luck-only maximum."""
    few = expected_max_sharpe(n_trials=5, sharpe_variance=1.0)
    many = expected_max_sharpe(n_trials=500, sharpe_variance=1.0)
    assert 0 < few < many


def test_expected_max_sharpe_is_zero_for_a_single_trial() -> None:
    assert expected_max_sharpe(n_trials=1, sharpe_variance=1.0) == 0.0


def test_expected_max_sharpe_scales_with_trial_spread() -> None:
    narrow = expected_max_sharpe(n_trials=100, sharpe_variance=0.25)
    wide = expected_max_sharpe(n_trials=100, sharpe_variance=4.0)
    assert wide > narrow


def test_expected_max_sharpe_from_observed_trials() -> None:
    sharpes = np.array([0.1, -0.2, 0.4, 0.0, 0.3, -0.1])
    from_values = expected_max_sharpe(sharpes)
    explicit = expected_max_sharpe(
        n_trials=len(sharpes), sharpe_variance=float(sharpes.var(ddof=1))
    )
    assert from_values == pytest.approx(explicit)


def test_deflated_sharpe_is_below_psr_when_many_trials_were_run() -> None:
    """Deflation must cost the strategy something for having been searched for."""
    rng = np.random.default_rng(5)
    returns = rng.normal(0.0008, 0.01, 3_000)
    trials = rng.normal(0, 0.5, 200)

    plain = probabilistic_sharpe(returns)
    deflated = deflated_sharpe(returns, trial_sharpes=trials)
    assert deflated < plain


def test_deflated_sharpe_penalises_a_wider_search() -> None:
    rng = np.random.default_rng(6)
    returns = rng.normal(0.0008, 0.01, 3_000)
    small = deflated_sharpe(returns, n_trials=5, sharpe_variance=0.25)
    large = deflated_sharpe(returns, n_trials=5_000, sharpe_variance=0.25)
    assert large < small


def test_minimum_track_record_length_shrinks_as_edge_grows() -> None:
    rng = np.random.default_rng(7)
    weak = minimum_track_record_length(rng.normal(0.0002, 0.01, 3_000))
    strong = minimum_track_record_length(rng.normal(0.0030, 0.01, 3_000))
    assert strong < weak


def test_minimum_track_record_is_infinite_without_an_edge() -> None:
    rng = np.random.default_rng(8)
    assert minimum_track_record_length(rng.normal(-0.001, 0.01, 1_000)) == float("inf")


def test_degenerate_inputs_return_nan_not_a_confident_number() -> None:
    assert math.isnan(sharpe_ratio([1.0]))
    assert math.isnan(sharpe_ratio([2.0, 2.0, 2.0]))
    assert math.isnan(probabilistic_sharpe([1.0, 2.0]))


# --------------------------------------------------------------- walk-forward


def make_frame(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    price = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    index = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
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


def test_train_and_test_windows_never_share_a_bar() -> None:
    """Label slicing is end-inclusive; masks must not be."""
    frame = make_frame(60 * 24 * 40)
    splits = walk_forward(frame.index, train="20D", test="5D", embargo="0D")
    assert splits
    for split in splits:
        train = frame.index[split.train_mask(frame.index)]
        test = frame.index[split.test_mask(frame.index)]
        assert train.intersection(test).empty


def test_walk_forward_reports_only_out_of_sample_results() -> None:
    from candlepilot.backtest.costs import CostModel
    from candlepilot.evaluate.sweep import run_walk_forward
    from candlepilot.strategies import DonchianBreakout

    frame = make_frame(60 * 24 * 40, seed=3)
    result = run_walk_forward(
        frame,
        DonchianBreakout,
        {"lookback": [60, 120]},
        train="20D",
        test="5D",
        embargo="1D",
        cost_model=CostModel(taker_fee=0.0, maker_fee=0.0, slippage=0.0),
    )

    assert len(result.splits) >= 2
    assert result.n_trials == 2
    # Every training trial is retained, not just the winners: the spread across
    # trials is what deflation needs.
    assert len(result.trial_sharpes) == result.n_trials * len(result.folds)

    report = result.report()
    assert report["oos_observations"] > 0
    assert report["trials_per_fold"] == 2


def test_walk_forward_refuses_when_no_fold_fits() -> None:
    from candlepilot.evaluate.sweep import run_walk_forward
    from candlepilot.strategies import DonchianBreakout

    frame = make_frame(60 * 24 * 2)
    with pytest.raises(ValueError, match="no walk-forward folds"):
        run_walk_forward(
            frame, DonchianBreakout, {"lookback": [60]}, train="90D", test="30D"
        )
