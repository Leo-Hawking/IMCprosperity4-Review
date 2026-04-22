"""纯 polars 过滤函数，供 ReviewContext / 绘图层调用。"""
from __future__ import annotations

from typing import Literal

import polars as pl


def filter_trades_by_qty(df: pl.DataFrame, allowed_qty: set[int] | None) -> pl.DataFrame:
    if allowed_qty is None:
        return df
    if df.is_empty():
        return df
    return df.filter(pl.col("quantity").abs().cast(pl.Int64).is_in(list(allowed_qty)))


def filter_orders_by_qty(
    df: pl.DataFrame,
    allowed_qty: set[int] | None,
    abs_value: bool = True,
) -> pl.DataFrame:
    if allowed_qty is None:
        return df
    if df.is_empty():
        return df
    col = pl.col("volume").abs() if abs_value else pl.col("volume")
    return df.filter(col.cast(pl.Int64).is_in(list(allowed_qty)))


def filter_trades_by_edge(
    df: pl.DataFrame,
    sign: Literal["pos", "neg", "zero", "nonneg"] | None,
) -> pl.DataFrame:
    if sign is None or df.is_empty() or "edge" not in df.columns:
        return df
    if sign == "pos":
        return df.filter(pl.col("edge") > 0)
    if sign == "neg":
        return df.filter(pl.col("edge") < 0)
    if sign == "zero":
        return df.filter(pl.col("edge") == 0)
    if sign == "nonneg":
        return df.filter(pl.col("edge") >= 0)
    return df


def filter_by_timestamp(
    df: pl.DataFrame,
    ts_min: int | None,
    ts_max: int | None,
) -> pl.DataFrame:
    if df.is_empty() or "timestamp" not in df.columns:
        return df
    out = df
    if ts_min is not None:
        out = out.filter(pl.col("timestamp") >= ts_min)
    if ts_max is not None:
        out = out.filter(pl.col("timestamp") <= ts_max)
    return out


def mark_cross_fair(ob_long: pl.DataFrame, fair_df: pl.DataFrame) -> pl.DataFrame:
    """bid > fair 或 ask < fair 的挂单标记 cross_fair=True。"""
    if ob_long.is_empty():
        return ob_long.with_columns(pl.lit(False).alias("cross_fair"))
    joined = ob_long.join(
        fair_df.select(["timestamp", "product", "fair"]),
        on=["timestamp", "product"],
        how="left",
    )
    return joined.with_columns(
        (
            ((pl.col("side") == "bid") & (pl.col("price") > pl.col("fair")))
            | ((pl.col("side") == "ask") & (pl.col("price") < pl.col("fair")))
        )
        .fill_null(False)
        .alias("cross_fair")
    )


def mark_near_fair(
    ob_long: pl.DataFrame,
    fair_df: pl.DataFrame,
    tick: int = 4,
) -> pl.DataFrame:
    """|price - fair| <= tick 且理性侧（bid <= fair / ask >= fair）标记 near_fair=True。"""
    if ob_long.is_empty():
        return ob_long.with_columns(pl.lit(False).alias("near_fair"))
    joined = ob_long.join(
        fair_df.select(["timestamp", "product", "fair"]),
        on=["timestamp", "product"],
        how="left",
    )
    rational = (
        ((pl.col("side") == "bid") & (pl.col("price") <= pl.col("fair")))
        | ((pl.col("side") == "ask") & (pl.col("price") >= pl.col("fair")))
    )
    within = (pl.col("price") - pl.col("fair")).abs() <= float(tick)
    return joined.with_columns((rational & within).fill_null(False).alias("near_fair"))
