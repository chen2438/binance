"""Walk-forward splits with an embargo.

A plain train/test cut leaks. A strategy evaluated on the first bar of the test
window reads a lookback that reaches back into training data, so the "out-of-sample"
period starts with a signal partly built from in-sample bars. The embargo is the gap
that removes the overlap, and it must be **at least the strategy's longest lookback**
— an embargo shorter than the lookback leaves exactly the leak it was meant to close.

Two modes:

* **rolling** — fixed-length training window, so the model is always fitted to recent
  data and old regimes drop out.
* **anchored** — training window grows from a fixed start, so every refit sees the
  whole history.

Neither is universally right, but they answer different questions and mixing them up
makes results incomparable, so the mode is explicit rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Split:
    """One walk-forward fold."""

    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    @property
    def embargo(self) -> pd.Timedelta:
        return self.test_start - self.train_end

    def train_mask(self, index: pd.DatetimeIndex) -> pd.Series:
        return (index >= self.train_start) & (index < self.train_end)

    def test_mask(self, index: pd.DatetimeIndex) -> pd.Series:
        return (index >= self.test_start) & (index < self.test_end)

    def __str__(self) -> str:
        return (
            f"train {self.train_start.date()}..{self.train_end.date()} | "
            f"test {self.test_start.date()}..{self.test_end.date()}"
        )


def walk_forward(
    index: pd.DatetimeIndex,
    *,
    train: str | pd.Timedelta,
    test: str | pd.Timedelta,
    embargo: str | pd.Timedelta = "0D",
    anchored: bool = False,
) -> list[Split]:
    """Generate walk-forward folds over a time index.

    ``embargo`` is the gap between the end of training and the start of testing. Set
    it to at least the longest lookback any candidate strategy uses.
    """
    if len(index) == 0:
        return []

    train_span = pd.Timedelta(train)
    test_span = pd.Timedelta(test)
    embargo_span = pd.Timedelta(embargo)
    if train_span <= pd.Timedelta(0) or test_span <= pd.Timedelta(0):
        raise ValueError("train and test spans must be positive")

    start = index.min()
    end = index.max()

    splits: list[Split] = []
    train_end = start + train_span
    while True:
        test_start = train_end + embargo_span
        test_end = test_start + test_span
        if test_start >= end:
            break
        splits.append(
            Split(
                train_start=start if anchored else train_end - train_span,
                train_end=train_end,
                test_start=test_start,
                test_end=min(test_end, end),
            )
        )
        if test_end >= end:
            break
        # The next fold trains up to the end of the window just tested, so every bar
        # is used for testing exactly once.
        train_end = test_end

    return splits
