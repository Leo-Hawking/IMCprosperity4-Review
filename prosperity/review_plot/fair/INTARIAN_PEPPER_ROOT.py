"""INTARIAN_PEPPER_ROOT fair: int(intercept + slope * timestamp)。"""
from __future__ import annotations

import polars as pl

PRODUCT = "INTARIAN_PEPPER_ROOT"


def compute_fair(
    ob_wide: pl.DataFrame,
    intercept: float = 11000.0,
    slope: float = 0.001,
) -> pl.DataFrame:
    if ob_wide.is_empty():
        return pl.DataFrame(
            schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64}
        )
    ts = (
        ob_wide.filter(pl.col("product") == PRODUCT)
        .select("timestamp")
        .unique()
        .sort("timestamp")
    )
    return ts.with_columns(
        [
            pl.lit(PRODUCT).alias("product"),
            (pl.lit(intercept) + pl.col("timestamp") * pl.lit(slope))
            .floor()
            .cast(pl.Float64)
            .alias("fair"),
        ]
    ).select(["timestamp", "product", "fair"])
