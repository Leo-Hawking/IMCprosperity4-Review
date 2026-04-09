"""Orderbook 分析工具：wall_mid、spread、adverse selection 等。"""

import numpy as np
import polars as pl


def compute_wall_mid(prices_wide: pl.DataFrame) -> pl.DataFrame:
    """
    从宽格式 prices 计算每个 timestamp 的 wall mid。

    Wall = bid/ask 侧挂单量最大的档位。
    Wall mid = (bid_wall + ask_wall) / 2
    """
    bid_cols = [(f"bid_price_{i}", f"bid_volume_{i}") for i in range(1, 4)]
    ask_cols = [(f"ask_price_{i}", f"ask_volume_{i}") for i in range(1, 4)]

    results = []
    for row in prices_wide.iter_rows(named=True):
        # 找 bid 侧 wall
        best_bid_price, best_bid_vol = None, 0
        for pc, vc in bid_cols:
            p, v = row.get(pc), row.get(vc)
            if p is not None and v is not None and v > best_bid_vol:
                best_bid_price, best_bid_vol = p, v

        # 找 ask 侧 wall
        best_ask_price, best_ask_vol = None, 0
        for pc, vc in ask_cols:
            p, v = row.get(pc), row.get(vc)
            if p is not None and v is not None and v > best_ask_vol:
                best_ask_price, best_ask_vol = p, v

        wall_mid = None
        if best_bid_price is not None and best_ask_price is not None:
            wall_mid = (best_bid_price + best_ask_price) / 2

        results.append({
            "day": row["day"],
            "timestamp": row["timestamp"],
            "product": row["product"],
            "bid_wall_price": best_bid_price,
            "bid_wall_volume": best_bid_vol,
            "ask_wall_price": best_ask_price,
            "ask_wall_volume": best_ask_vol,
            "wall_mid": wall_mid,
            "raw_mid": row.get("mid_price"),
        })

    return pl.DataFrame(results)


def compute_spread(prices_wide: pl.DataFrame) -> pl.DataFrame:
    """计算 raw spread (best_ask - best_bid) 和 wall spread。"""
    walls = compute_wall_mid(prices_wide)
    return walls.with_columns([
        (pl.col("ask_wall_price") - pl.col("bid_wall_price")).alias("wall_spread"),
    ]).select([
        "day", "timestamp", "product",
        "wall_mid", "raw_mid", "wall_spread",
        "bid_wall_price", "ask_wall_price",
    ])


def return_autocorrelation(wall_mid_array: np.ndarray, max_lag: int = 20):
    """
    Wall mid return 的自相关分析。

    返回: (autocorrs, ci_95)
    - autocorrs: lag 1..max_lag 的自相关系数
    - ci_95: 白噪声假设下的 95% 置信区间
    """
    returns = np.diff(wall_mid_array)
    returns = returns[~np.isnan(returns)]
    n = len(returns)

    autocorrs = []
    for lag in range(1, max_lag + 1):
        if lag >= n:
            autocorrs.append(np.nan)
            continue
        c = np.corrcoef(returns[:-lag], returns[lag:])[0, 1]
        autocorrs.append(c)

    ci = 1.96 / np.sqrt(n)
    return autocorrs, ci


def adverse_selection(
    trades: pl.DataFrame,
    wall_mid_series: pl.DataFrame,
    horizons: list[int] = [1, 5, 10, 50],
) -> dict[int, float]:
    """
    成交方向 vs 后续价格移动。

    wall_mid_series: 需要 timestamp, wall_mid 两列。
    trades: 需要 timestamp, quantity 列（正=buyer initiated）。

    返回: {horizon: mean_pnl_after}
    正值 = taker 有信息 (adverse selection)
    零 = taker 无信息 (做市安全)
    负值 = taker 是噪声 (做市有利)
    """
    wm = wall_mid_series.sort("timestamp")
    ts_arr = wm["timestamp"].to_numpy()
    mid_arr = wm["wall_mid"].to_numpy()

    results = {}
    for h in horizons:
        pnl_list = []
        for row in trades.iter_rows(named=True):
            t = row["timestamp"]
            idx = np.searchsorted(ts_arr, t)
            idx_h = np.searchsorted(ts_arr, t + h * 100)  # 100ms per step
            if idx >= len(mid_arr) or idx_h >= len(mid_arr):
                continue
            direction = 1 if row["quantity"] > 0 else -1
            move = mid_arr[idx_h] - mid_arr[idx]
            pnl_list.append(direction * move)

        results[h] = float(np.mean(pnl_list)) if pnl_list else np.nan
    return results


def order_interval_distribution(
    prices_long: pl.DataFrame,
    product: str | None = None,
    day: int | None = None,
    side: str | None = None,
    min_order_volume: float | None = None,
) -> pl.DataFrame:
    """
    订单事件时间间隔分布（ms）。

    以 prices_long 的每条挂单记录作为事件，按 timestamp 去重后计算相邻间隔。
    """
    p = prices_long
    if product is not None:
        p = p.filter(pl.col("product") == product)
    if day is not None and "day" in p.columns:
        p = p.filter(pl.col("day") == day)
    if side is not None:
        p = p.filter(pl.col("side") == side)
    if min_order_volume is not None and "volume" in p.columns:
        p = p.filter(pl.col("volume") >= min_order_volume)

    if p.is_empty():
        return pl.DataFrame({"timestamp": [], "interval_ms": []})

    ts = p.select("timestamp").unique().sort("timestamp")
    return ts.with_columns((pl.col("timestamp") - pl.col("timestamp").shift(1)).alias("interval_ms")).drop_nulls("interval_ms")


def trade_interval_distribution(
    trades: pl.DataFrame,
    product: str | None = None,
    day: int | None = None,
    min_trade_quantity: float | None = None,
) -> pl.DataFrame:
    """成交事件时间间隔分布（ms）。"""
    t = trades
    if product is not None and "product" in t.columns:
        t = t.filter(pl.col("product") == product)
    if day is not None and "day" in t.columns:
        t = t.filter(pl.col("day") == day)
    if min_trade_quantity is not None:
        qty_col = "quantity" if "quantity" in t.columns else ("volume" if "volume" in t.columns else None)
        if qty_col is not None:
            t = t.filter(pl.col(qty_col).abs() >= min_trade_quantity)

    if t.is_empty():
        return pl.DataFrame({"timestamp": [], "interval_ms": []})

    ts = t.select("timestamp").unique().sort("timestamp")
    return ts.with_columns((pl.col("timestamp") - pl.col("timestamp").shift(1)).alias("interval_ms")).drop_nulls("interval_ms")


def normalized_level_trade_stats(
    trades: pl.DataFrame,
    wall_mid_df: pl.DataFrame,
    product: str | None = None,
    day: int | None = None,
    round_to: int | None = None,
) -> pl.DataFrame:
    """
    标准化后不同价位（price - wall_mid）的成交统计。

    返回列:
    - norm_level: 归一化价位
    - trade_count: 成交次数
    - total_quantity: 总成交量（绝对值）
    - avg_quantity: 单笔平均成交量（绝对值）
    """
    t = trades
    wm = wall_mid_df

    if product is not None and "product" in t.columns:
        t = t.filter(pl.col("product") == product)
    if product is not None and "product" in wm.columns:
        wm = wm.filter(pl.col("product") == product)
    if day is not None and "day" in t.columns:
        t = t.filter(pl.col("day") == day)
    if day is not None and "day" in wm.columns:
        wm = wm.filter(pl.col("day") == day)

    if t.is_empty() or wm.is_empty():
        return pl.DataFrame({"norm_level": [], "trade_count": [], "total_quantity": [], "avg_quantity": []})

    qty_col = "quantity" if "quantity" in t.columns else ("volume" if "volume" in t.columns else None)
    if qty_col is None:
        return pl.DataFrame({"norm_level": [], "trade_count": [], "total_quantity": [], "avg_quantity": []})

    joined = (
        t.join(wm.select(["timestamp", "product", "wall_mid"]), on=["timestamp", "product"], how="left")
        .filter(pl.col("wall_mid").is_not_null())
        .with_columns((pl.col("price") - pl.col("wall_mid")).alias("norm_price"))
    )

    if joined.is_empty():
        return pl.DataFrame({"norm_level": [], "trade_count": [], "total_quantity": [], "avg_quantity": []})

    if round_to is not None and round_to >= 0:
        joined = joined.with_columns(pl.col("norm_price").round(round_to).alias("norm_level"))
    else:
        joined = joined.with_columns(pl.col("norm_price").alias("norm_level"))

    return (
        joined.group_by("norm_level")
        .agg([
            pl.len().alias("trade_count"),
            pl.col(qty_col).abs().sum().alias("total_quantity"),
            pl.col(qty_col).abs().mean().alias("avg_quantity"),
        ])
        .sort("norm_level")
    )
