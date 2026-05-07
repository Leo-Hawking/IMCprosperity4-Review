"""通用 fair helpers —— 当前仅提供 mid_price / wall_mid 作为 placeholder。

任何产品级 fair 计算都放在 `fair/<PRODUCT>.py` 里，实现
`compute_fair(ob_wide: pl.DataFrame, **params) -> pl.DataFrame[(timestamp, product, fair)]`。
"""
from __future__ import annotations

import polars as pl


def _max_volume_side_price(df: pl.DataFrame, side: str) -> pl.Expr:
    """Pick side price at the level with the largest displayed volume.

    Tie-break rule: lower level index wins (L1 > L2 > L3).
    """
    v1 = pl.col(f"{side}_volume_1").fill_null(-1)
    v2 = pl.col(f"{side}_volume_2").fill_null(-1)
    v3 = pl.col(f"{side}_volume_3").fill_null(-1)
    p1 = pl.col(f"{side}_price_1")
    p2 = pl.col(f"{side}_price_2")
    p3 = pl.col(f"{side}_price_3")
    chosen = (
        pl.when((v1 >= v2) & (v1 >= v3))
        .then(p1)
        .when(v2 >= v3)
        .then(p2)
        .otherwise(p3)
    )
    return pl.coalesce([chosen, p1, p2, p3])


def wall_mid(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    """Use max-volume bid/ask level midpoint as wall-mid fair."""
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})
    df = ob_wide.filter(pl.col("product") == product)
    bid_wall = _max_volume_side_price(df, "bid")
    ask_wall = _max_volume_side_price(df, "ask")
    fair = ((bid_wall + ask_wall) / 2).alias("fair")
    keep = [c for c in ("day", "timestamp", "global_ts") if c in df.columns]
    return df.with_columns(fair).select([*keep, "product", "fair"])


def mid_price(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    """用 `mid_price` 列（csv 自带）作为 placeholder fair。"""
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})
    df = ob_wide.filter(pl.col("product") == product)
    keep = [c for c in ("day", "timestamp", "global_ts") if c in df.columns]
    return df.select([*keep, "product", pl.col("mid_price").alias("fair")])


def _round5_wall_side(df: pl.DataFrame, side: str) -> pl.Expr:
    v1 = pl.col(f"{side}_volume_1").cast(pl.Float64)
    v2 = pl.col(f"{side}_volume_2").cast(pl.Float64)
    v3 = pl.col(f"{side}_volume_3").cast(pl.Float64)
    p1 = pl.col(f"{side}_price_1").cast(pl.Float64)
    p2 = pl.col(f"{side}_price_2").cast(pl.Float64)
    p3 = pl.col(f"{side}_price_3").cast(pl.Float64)

    v1f = v1.fill_null(float("-inf"))
    v2f = v2.fill_null(float("-inf"))
    v3f = v3.fill_null(float("-inf"))
    side_present = v1.is_not_null() | v2.is_not_null() | v3.is_not_null()

    chosen = (
        pl.when((v1f >= v2f) & (v1f >= v3f))
        .then(p1)
        .when(v2f >= v3f)
        .then(p2)
        .otherwise(p3)
    )
    return pl.when(side_present).then(chosen).otherwise(None)


def round5_fair(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    """Round 5 fair price from wall mid + near-wall rule.

    - wall_mid = (max-volume bid + max-volume ask) / 2
    - If exactly one quote has |price - wall_mid| <= 1, fair = that price
    - Otherwise, fair = wall_mid - 0.5
    - If one/both sides are missing, forward-fill by product
    """
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})

    df = ob_wide.filter(pl.col("product") == product)
    if df.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})

    wall_bid = _round5_wall_side(df, "bid")
    wall_ask = _round5_wall_side(df, "ask")
    wall_mid = ((wall_bid + wall_ask) / 2.0).alias("wall_mid")

    df = df.with_columns([
        wall_bid.alias("wall_bid"),
        wall_ask.alias("wall_ask"),
        wall_mid,
    ])

    near_exprs = []
    for side in ("bid", "ask"):
        for lvl in (1, 2, 3):
            price_col = pl.col(f"{side}_price_{lvl}").cast(pl.Float64)
            near = (price_col - pl.col("wall_mid")).abs() <= 1
            near = near & price_col.is_not_null() & pl.col("wall_mid").is_not_null()
            near_exprs.append(near)

    near_count = pl.sum_horizontal([e.cast(pl.Int64) for e in near_exprs])

    near_price = pl.coalesce([
        pl.when(near_exprs[0]).then(pl.col("bid_price_1")),
        pl.when(near_exprs[1]).then(pl.col("bid_price_2")),
        pl.when(near_exprs[2]).then(pl.col("bid_price_3")),
        pl.when(near_exprs[3]).then(pl.col("ask_price_1")),
        pl.when(near_exprs[4]).then(pl.col("ask_price_2")),
        pl.when(near_exprs[5]).then(pl.col("ask_price_3")),
    ])

    df = df.with_columns([
        near_count.alias("near_count"),
        pl.when(near_count == 1)
        .then(near_price)
        .otherwise(pl.col("wall_mid") - 0.5)
        .alias("fair_raw"),
    ])

    df = df.sort(["day", "timestamp"]).with_columns([
        pl.col("wall_mid").fill_null(strategy="forward").alias("wall_mid"),
        pl.col("fair_raw").fill_null(strategy="forward").alias("fair"),
    ])

    keep = [c for c in ("day", "timestamp", "global_ts") if c in df.columns]
    return df.select([*keep, "product", "fair"])
