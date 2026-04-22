"""ASH_COATED_OSMIUM fair: max-vol>20 两侧中点 + forward-fill。"""
from __future__ import annotations

import polars as pl

from .base import forward_fill_two_sides

PRODUCT = "ASH_COATED_OSMIUM"


def compute_fair(ob_wide: pl.DataFrame, vol_threshold: float = 20.0) -> pl.DataFrame:
    return forward_fill_two_sides(
        ob_wide,
        product=PRODUCT,
        vol_threshold=vol_threshold,
        mid_fallback=True,
    )
