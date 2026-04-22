"""plots 层共享辅助: 时间窗口裁剪 + resample context 管理。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

import polars as pl

from ..filters import filter_by_timestamp
from ..resample import no_resample


def clip_ts(df: pl.DataFrame, ts_range: tuple[Optional[int], Optional[int]]) -> pl.DataFrame:
    return filter_by_timestamp(df, ts_range[0], ts_range[1])


@contextmanager
def maybe_no_resample(full_resolution: bool):
    if full_resolution:
        with no_resample():
            yield
    else:
        yield
