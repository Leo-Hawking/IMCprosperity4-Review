"""通用 fair helpers —— 当前仅提供 mid_price / wall_mid 作为 placeholder。

任何产品级 fair 计算都放在 `fair/<PRODUCT>.py` 里，实现
`compute_fair(ob_wide: pl.DataFrame, **params) -> pl.DataFrame[(timestamp, product, fair)]`。
"""
from __future__ import annotations

import polars as pl


def wall_mid(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    """用 best bid / best ask 中点作为 placeholder fair。"""
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})
    df = ob_wide.filter(pl.col("product") == product)
    fair = ((pl.col("bid_price_1") + pl.col("ask_price_1")) / 2).alias("fair")
    keep = [c for c in ("day", "timestamp", "global_ts") if c in df.columns]
    return df.with_columns(fair).select([*keep, "product", "fair"])


def mid_price(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    """用 `mid_price` 列（csv 自带）作为 placeholder fair。"""
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})
    df = ob_wide.filter(pl.col("product") == product)
    keep = [c for c in ("day", "timestamp", "global_ts") if c in df.columns]
    return df.select([*keep, "product", pl.col("mid_price").alias("fair")])
