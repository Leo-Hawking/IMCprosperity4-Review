"""HYDROGEL_PACK fair: 固定 10000（与 mean_reversion_simple.py 中 MU 对齐）。"""
from __future__ import annotations

import polars as pl

PRODUCT = "HYDROGEL_PACK"


def compute_fair(ob_wide: pl.DataFrame, mu: float = 10000.0) -> pl.DataFrame:
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
    return ts.with_columns([
        pl.lit(PRODUCT).alias("product"),
        pl.lit(float(mu)).alias("fair"),
    ]).select(["timestamp", "product", "fair"])
