"""纯函数过滤器 —— 按 day / timestamp / product / volume 切片 DataFrame。"""
from __future__ import annotations

import polars as pl


def by_product(df: pl.DataFrame, products: list[str] | str) -> pl.DataFrame:
    if df.is_empty() or "product" not in df.columns:
        return df
    if isinstance(products, str):
        return df.filter(pl.col("product") == products)
    return df.filter(pl.col("product").is_in(products))


def by_day(df: pl.DataFrame, days: list[int] | int) -> pl.DataFrame:
    if df.is_empty() or "day" not in df.columns:
        return df
    if isinstance(days, int):
        return df.filter(pl.col("day") == days)
    return df.filter(pl.col("day").is_in(days))


def by_ts(
    df: pl.DataFrame,
    ts_min: int | None = None,
    ts_max: int | None = None,
    *,
    column: str = "global_ts",
) -> pl.DataFrame:
    if df.is_empty() or column not in df.columns:
        return df
    if ts_min is not None:
        df = df.filter(pl.col(column) >= ts_min)
    if ts_max is not None:
        df = df.filter(pl.col(column) <= ts_max)
    return df


def by_volume(df: pl.DataFrame, min_vol: int = 0, column: str = "volume") -> pl.DataFrame:
    if df.is_empty() or column not in df.columns:
        return df
    return df.filter(pl.col(column).abs() >= min_vol)
