"""在 ob_wide / ob_long 上附加 TTE、moneyness、spread 等派生列。

严格不做 fair value / IV 计算（那些要走 `vev_plot.fair`）。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from .dataio import DAY_START_TTE, TICKS_PER_DAY, UNDERLYING, VOUCHER_SYMBOLS


def attach_tte(df: pl.DataFrame) -> pl.DataFrame:
    """按 (day, timestamp) 计算剩余到期天数，写入 `tte_days` 和 `tte_years` 两列。"""
    if df.is_empty() or "day" not in df.columns:
        return df
    tte_days = (
        pl.col("day").cast(pl.Int64).map_elements(lambda d: DAY_START_TTE.get(int(d), 0), return_dtype=pl.Float64)
        - pl.col("timestamp").cast(pl.Float64) / TICKS_PER_DAY
    )
    return df.with_columns(
        tte_days.alias("tte_days"),
        (tte_days / 365).alias("tte_years"),
    )


def attach_strike(df: pl.DataFrame) -> pl.DataFrame:
    """从 VEV_<K> 产品名解析出 strike（非 voucher 产品该列为 null）。"""
    if df.is_empty():
        return df
    return df.with_columns(
        pl.when(pl.col("product").str.starts_with("VEV_"))
        .then(pl.col("product").str.extract(r"VEV_(\d+)").cast(pl.Int64))
        .otherwise(None)
        .alias("strike")
    )


def attach_spread(ob_wide: pl.DataFrame) -> pl.DataFrame:
    """ob_wide 附加 `spread_1 = ask1 - bid1` 和 wall_mid。"""
    if ob_wide.is_empty():
        return ob_wide
    bid_wall = pl.coalesce([
        pl.when(
            (pl.col("bid_volume_1").fill_null(-1) >= pl.col("bid_volume_2").fill_null(-1))
            & (pl.col("bid_volume_1").fill_null(-1) >= pl.col("bid_volume_3").fill_null(-1))
        )
        .then(pl.col("bid_price_1"))
        .when(pl.col("bid_volume_2").fill_null(-1) >= pl.col("bid_volume_3").fill_null(-1))
        .then(pl.col("bid_price_2"))
        .otherwise(pl.col("bid_price_3")),
        pl.col("bid_price_1"),
        pl.col("bid_price_2"),
        pl.col("bid_price_3"),
    ])
    ask_wall = pl.coalesce([
        pl.when(
            (pl.col("ask_volume_1").fill_null(-1) >= pl.col("ask_volume_2").fill_null(-1))
            & (pl.col("ask_volume_1").fill_null(-1) >= pl.col("ask_volume_3").fill_null(-1))
        )
        .then(pl.col("ask_price_1"))
        .when(pl.col("ask_volume_2").fill_null(-1) >= pl.col("ask_volume_3").fill_null(-1))
        .then(pl.col("ask_price_2"))
        .otherwise(pl.col("ask_price_3")),
        pl.col("ask_price_1"),
        pl.col("ask_price_2"),
        pl.col("ask_price_3"),
    ])
    return ob_wide.with_columns(
        (pl.col("ask_price_1") - pl.col("bid_price_1")).alias("spread_1"),
        ((ask_wall + bid_wall) / 2).alias("wall_mid"),
    )


def attach_moneyness(
    ob_wide: pl.DataFrame,
    *,
    underlying: str = UNDERLYING,
) -> pl.DataFrame:
    """对 voucher 行附加 `moneyness = ln(K / S_t) / sqrt(T)`，S_t 取同时刻 underlying mid。

    这里 S_t 用 underlying 的 mid_price，不涉及 "fair" 概念。
    """
    if ob_wide.is_empty():
        return ob_wide
    df = ob_wide
    if "strike" not in df.columns:
        df = attach_strike(df)
    if "tte_days" not in df.columns:
        df = attach_tte(df)

    und = (
        df.filter(pl.col("product") == underlying)
        .select(["day", "timestamp", pl.col("mid_price").alias("underlying_mid")])
    )
    df = df.join(und, on=["day", "timestamp"], how="left")

    m = (
        (pl.col("strike").cast(pl.Float64).log() - pl.col("underlying_mid").log())
        / pl.col("tte_years").sqrt()
    )
    return df.with_columns(m.alias("moneyness"))


# -------- trades enrichment --------

def attach_trade_flow(trades: pl.DataFrame) -> pl.DataFrame:
    """trades 加 `signed_qty`（若 buyer/seller 能判断 taker 方向）和 `notional`。"""
    if trades.is_empty():
        return trades
    signed = (
        pl.when(pl.col("buyer").fill_null("") != "")
        .then(pl.col("quantity"))
        .when(pl.col("seller").fill_null("") != "")
        .then(-pl.col("quantity"))
        .otherwise(pl.col("quantity"))
    )
    return trades.with_columns(
        signed.alias("signed_qty"),
        (pl.col("price") * pl.col("quantity")).alias("notional"),
    )
