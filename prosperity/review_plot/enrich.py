"""上下游数据加工: 仓位、分产品 PnL、my_with_fair。"""
from __future__ import annotations

import polars as pl


def compute_position_df(my_trades: pl.DataFrame, products: list[str]) -> pl.DataFrame:
    """逐笔累计仓位，每个产品以 t=0, pos=0 起始。"""
    rows: list[dict] = []
    for product in products:
        rows.append({"timestamp": 0, "product": product, "position": 0})
        if my_trades.is_empty():
            continue
        pt = my_trades.filter(pl.col("product") == product).sort("timestamp")
        pos = 0
        for r in pt.iter_rows(named=True):
            signed = int(r["quantity"]) if r["trade_type"] == "my_buy" else -int(r["quantity"])
            pos += signed
            rows.append({"timestamp": int(r["timestamp"]), "product": product, "position": pos})
    return pl.DataFrame(
        rows,
        schema={"timestamp": pl.Int64, "product": pl.String, "position": pl.Int64},
    ).sort(["product", "timestamp"])


def compute_pnl_by_product(ob_wide: pl.DataFrame) -> pl.DataFrame:
    """从 activitiesLog 的 profit_and_loss 列抽出分产品 PnL 曲线。"""
    if ob_wide.is_empty() or "profit_and_loss" not in ob_wide.columns:
        return pl.DataFrame(
            schema={
                "timestamp": pl.Int64,
                "product": pl.String,
                "profit_and_loss": pl.Float64,
            }
        )
    return (
        ob_wide.select(["timestamp", "product", "profit_and_loss"])
        .sort(["product", "timestamp"])
    )


def attach_fair_and_edge(
    my_trades: pl.DataFrame,
    fair_df: pl.DataFrame,
) -> pl.DataFrame:
    """给我方成交 join fair，算 edge / edge_pnl / delta。"""
    if my_trades.is_empty():
        return my_trades.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("fair"),
                pl.lit(None, dtype=pl.Float64).alias("edge"),
                pl.lit(None, dtype=pl.Float64).alias("edge_pnl"),
                pl.lit(None, dtype=pl.Float64).alias("delta"),
            ]
        )
    return (
        my_trades.join(
            fair_df.select(["timestamp", "product", "fair"]),
            on=["timestamp", "product"],
            how="left",
        )
        .with_columns(
            pl.when(pl.col("trade_type") == "my_buy")
            .then(pl.col("fair") - pl.col("price"))
            .otherwise(pl.col("price") - pl.col("fair"))
            .alias("edge")
        )
        .with_columns(
            [
                (pl.col("edge") * pl.col("quantity")).alias("edge_pnl"),
                (pl.col("price") - pl.col("fair")).alias("delta"),
            ]
        )
    )


def attach_delta_to_trades(
    trades: pl.DataFrame,
    fair_df: pl.DataFrame,
) -> pl.DataFrame:
    """仅附加 fair 和 delta (price - fair)，用于市场成交标注。"""
    if trades.is_empty():
        return trades.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("fair"),
                pl.lit(None, dtype=pl.Float64).alias("delta"),
            ]
        )
    return trades.join(
        fair_df.select(["timestamp", "product", "fair"]),
        on=["timestamp", "product"],
        how="left",
    ).with_columns((pl.col("price") - pl.col("fair")).alias("delta"))


def attach_delta_to_orders(
    ob_long: pl.DataFrame,
    fair_df: pl.DataFrame,
) -> pl.DataFrame:
    """给 orderbook 挂单附加 fair 和 delta (price - fair)。"""
    if ob_long.is_empty():
        return ob_long.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("fair"),
                pl.lit(None, dtype=pl.Float64).alias("delta"),
            ]
        )
    return ob_long.join(
        fair_df.select(["timestamp", "product", "fair"]),
        on=["timestamp", "product"],
        how="left",
    ).with_columns((pl.col("price") - pl.col("fair")).alias("delta"))
