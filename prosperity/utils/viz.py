"""通用绘图函数（plotly）。"""

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import polars as pl
import numpy as np

try:
    from plotly_resampler import FigureResampler
except ImportError:
    FigureResampler = None


def _new_figure(use_resampler: bool = True):
    fig = go.Figure()
    if use_resampler and FigureResampler is not None:
        return FigureResampler(fig)
    return fig


def _wrap_with_resampler(fig, use_resampler: bool = True):
    if use_resampler and FigureResampler is not None:
        return FigureResampler(fig)
    return fig


def plot_orderbook_scatter(
    prices_long: pl.DataFrame,
    trades: pl.DataFrame | None = None,
    product: str | None = None,
    day: int | None = None,
    wall_mid_df: pl.DataFrame | None = None,
    start_ts: int | float | None = None,
    end_ts: int | float | None = None,
    filter_by_volume: bool = False,
    min_order_volume: float | None = None,
    min_trade_quantity: float | None = None,
    use_resampler: bool = True,
    height: int = 600,
) -> go.Figure:
    """
    Orderbook 散点时序图（核心可视化）。

    - 蓝点 = bid 档位，红点 = ask 档位，点大小 ∝ 挂单量
    - 黑 x = 成交
    - 绿线 = wall_mid（如果提供）
    """
    p = prices_long
    if product:
        p = p.filter(pl.col("product") == product)
    if day is not None:
        p = p.filter(pl.col("day") == day)
    if filter_by_volume and min_order_volume is not None and "volume" in p.columns:
        p = p.filter(pl.col("volume") >= min_order_volume)

    t = None
    if trades is not None:
        t = trades
        if product:
            t = t.filter(pl.col("product") == product)
        if day is not None and "day" in t.columns:
            t = t.filter(pl.col("day") == day)
        if filter_by_volume and min_trade_quantity is not None:
            trade_qty_col = "quantity" if "quantity" in t.columns else ("volume" if "volume" in t.columns else None)
            if trade_qty_col is not None:
                t = t.filter(pl.col(trade_qty_col).abs() >= min_trade_quantity)

    wm = None
    if wall_mid_df is not None:
        wm = wall_mid_df
        if product:
            wm = wm.filter(pl.col("product") == product)
        if day is not None:
            wm = wm.filter(pl.col("day") == day)
        wm = wm.sort("timestamp")

    # 统一按 timestamp 截断，保证 orderbook / trades / wall_mid 同步对齐。
    if start_ts is not None:
        p = p.filter(pl.col("timestamp") >= start_ts)
        if t is not None:
            t = t.filter(pl.col("timestamp") >= start_ts)
        if wm is not None:
            wm = wm.filter(pl.col("timestamp") >= start_ts)
    if end_ts is not None:
        p = p.filter(pl.col("timestamp") <= end_ts)
        if t is not None:
            t = t.filter(pl.col("timestamp") <= end_ts)
        if wm is not None:
            wm = wm.filter(pl.col("timestamp") <= end_ts)

    fig = _new_figure(use_resampler=use_resampler)

    # Bids
    bids = p.filter(pl.col("side") == "bid")
    max_vol = p["volume"].max() or 1
    fig.add_trace(go.Scatter(
        x=bids["timestamp"].to_list(),
        y=bids["price"].to_list(),
        mode="markers",
        marker=dict(
            color="steelblue",
            size=[v / max_vol * 15 for v in bids["volume"].to_list()],
            opacity=0.5,
        ),
        customdata=bids["volume"].to_list(),
        hovertemplate="Timestamp=%{x}<br>Price=%{y}<br>挂单量=%{customdata}<extra></extra>",
        name="Bids",
    ))

    # Asks
    asks = p.filter(pl.col("side") == "ask")
    fig.add_trace(go.Scatter(
        x=asks["timestamp"].to_list(),
        y=asks["price"].to_list(),
        mode="markers",
        marker=dict(
            color="salmon",
            size=[v / max_vol * 15 for v in asks["volume"].to_list()],
            opacity=0.5,
        ),
        customdata=asks["volume"].to_list(),
        hovertemplate="Timestamp=%{x}<br>Price=%{y}<br>挂单量=%{customdata}<extra></extra>",
        name="Asks",
    ))

    # Irrational / near-fair order markers (only when fair reference is available).
    if wm is not None:
        p_with_fair = (
            p.join(wm.select(["timestamp", "product", "wall_mid"]), on=["timestamp", "product"], how="left")
            .filter(pl.col("wall_mid").is_not_null())
            .with_columns((pl.col("price") - pl.col("wall_mid")).alias("norm_price"))
        )

        irrational_orders = p_with_fair.filter(
            ((pl.col("side") == "bid") & (pl.col("norm_price") > 0))
            | ((pl.col("side") == "ask") & (pl.col("norm_price") < 0))
        )
        if irrational_orders.height > 0:
            irr_sizes = [
                max(10, min(28, abs(v) / max_vol * 30)) if v is not None else 10
                for v in irrational_orders["volume"].to_list()
            ]
            fig.add_trace(go.Scatter(
                x=irrational_orders["timestamp"].to_list(),
                y=irrational_orders["price"].to_list(),
                mode="markers",
                marker=dict(
                    color="gold",
                    symbol="x",
                    size=irr_sizes,
                    line=dict(width=1, color="black"),
                ),
                name="Cross-Fair Orders",
                customdata=list(zip(
                    irrational_orders["norm_price"].to_list(),
                    irrational_orders["price"].to_list(),
                    irrational_orders["volume"].to_list(),
                )),
                hovertemplate="t=%{x}<br>p=%{y}<br>Δ=%{customdata[0]}<br>px=%{customdata[1]}<br>qty=%{customdata[2]}<extra>Cross-Fair</extra>",
            ))

        near_fair_orders = p_with_fair.filter(
            (pl.col("norm_price").abs() <= 4)
            & (
                ((pl.col("side") == "bid") & (pl.col("norm_price") <= 0))
                | ((pl.col("side") == "ask") & (pl.col("norm_price") >= 0))
            )
        )
        if near_fair_orders.height > 0:
            near_sizes = [
                max(8, min(20, abs(v) / max_vol * 18)) if v is not None else 8
                for v in near_fair_orders["volume"].to_list()
            ]
            fig.add_trace(go.Scatter(
                x=near_fair_orders["timestamp"].to_list(),
                y=near_fair_orders["price"].to_list(),
                mode="markers",
                marker=dict(
                    color="purple",
                    symbol="diamond",
                    size=near_sizes,
                    line=dict(width=1, color="indigo"),
                    opacity=0.9,
                ),
                name="Near-Fair Rational Orders (|Δ|<=4)",
                customdata=list(zip(
                    near_fair_orders["norm_price"].to_list(),
                    near_fair_orders["price"].to_list(),
                    near_fair_orders["volume"].to_list(),
                )),
                hovertemplate="t=%{x}<br>p=%{y}<br>Δ=%{customdata[0]}<br>px=%{customdata[1]}<br>qty=%{customdata[2]}<extra>Near-Fair</extra>",
            ))

    # Trades
    if t is not None:
        trade_qty_col = "quantity" if "quantity" in t.columns else ("volume" if "volume" in t.columns else None)
        if trade_qty_col is not None and t.height > 0:
            q_abs = t[trade_qty_col].abs()
            trade_max = q_abs.max() or 1
            trade_sizes = [4 + (v / trade_max) * 12 for v in q_abs.to_list()]
        else:
            trade_sizes = 8
        trade_qty = t[trade_qty_col].to_list() if trade_qty_col is not None else [None] * t.height
        fig.add_trace(go.Scatter(
            x=t["timestamp"].to_list(),
            y=t["price"].to_list(),
            mode="markers",
            marker=dict(color="black", symbol="x", size=trade_sizes),
            customdata=trade_qty,
            hovertemplate="Timestamp=%{x}<br>Price=%{y}<br>成交量=%{customdata}<extra></extra>",
            name="Trades",
        ))

    # Wall mid line
    if wm is not None:
        fig.add_trace(go.Scatter(
            x=wm["timestamp"].to_list(),
            y=wm["wall_mid"].to_list(),
            mode="lines",
            line=dict(color="green", width=1.5),
            name="Wall Mid",
        ))

    title = f"{product or 'All'}" + (f" Day {day}" if day is not None else "")
    fig.update_layout(title=title, height=height, xaxis_title="Timestamp", yaxis_title="Price")
    return fig


def plot_autocorrelation(autocorrs: list[float], ci: float, title: str = "Return ACF") -> go.Figure:
    """自相关柱状图 + 95% 置信带。"""
    lags = list(range(1, len(autocorrs) + 1))
    fig = go.Figure()
    fig.add_trace(go.Bar(x=lags, y=autocorrs, name="ACF", marker_color="steelblue"))
    fig.add_hline(y=ci, line_dash="dash", line_color="red", annotation_text="95% CI")
    fig.add_hline(y=-ci, line_dash="dash", line_color="red")
    fig.add_hline(y=0, line_color="gray")
    fig.update_layout(title=title, xaxis_title="Lag", yaxis_title="Autocorrelation", height=400)
    return fig


def plot_interval_distribution(
    intervals_df: pl.DataFrame,
    interval_col: str = "interval_ms",
    nbins: int = 80,
    title: str = "Interval Distribution",
    wall_mid_df: pl.DataFrame | None = None,
    wall_mid_value_col: str = "wall_mid",
    height: int = 400,
) -> go.Figure:
    """间隔分布直方图。"""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    values = intervals_df[interval_col].to_list() if interval_col in intervals_df.columns else []
    fig.add_trace(go.Histogram(x=values, nbinsx=nbins, marker_color="steelblue", name="count"), secondary_y=False)
    if wall_mid_df is not None and {"timestamp", wall_mid_value_col}.issubset(set(wall_mid_df.columns)):
        wm = wall_mid_df.sort("timestamp")
        fig.add_trace(
            go.Scatter(
                x=wm["timestamp"].to_list(),
                y=wm[wall_mid_value_col].to_list(),
                mode="lines",
                line=dict(color="green", width=1.5),
                name="Wall Mid Trend",
                yaxis="y2",
            ),
            secondary_y=True,
        )
    fig.update_layout(title=title, xaxis_title="Interval (ms)", yaxis_title="Count", height=height)
    fig.update_yaxes(title_text="Count", secondary_y=False)
    fig.update_yaxes(title_text="Wall Mid", secondary_y=True)
    return fig


def plot_trade_profile(trades: pl.DataFrame, product: str | None = None) -> go.Figure:
    """每个交易者的成交量分布。"""
    t = trades
    if product:
        t = t.filter(pl.col("product") == product)

    # 按 buyer 聚合
    buy_stats = (
        t.filter(pl.col("buyer").is_not_null() & (pl.col("buyer") != ""))
        .group_by("buyer")
        .agg([
            pl.col("quantity").count().alias("count"),
            pl.col("quantity").sum().alias("total_qty"),
            pl.col("quantity").mean().alias("avg_qty"),
        ])
        .sort("total_qty", descending=True)
    )

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=buy_stats["buyer"].to_list(),
        y=buy_stats["total_qty"].to_list(),
        name="Total Buy Qty",
    ))
    fig.update_layout(title=f"Trade Profile {product or ''}", height=400)
    return fig


def plot_normalized_orderbook(
    prices_long: pl.DataFrame,
    wall_mid_df: pl.DataFrame,
    product: str,
    day: int | None = None,
    trades: pl.DataFrame | None = None,
    filter_by_volume: bool = False,
    min_order_volume: float | None = None,
    min_trade_quantity: float | None = None,
    overlay_wall_mid_trend: bool = False,
    overlay_raw_wall_mid: bool = True,
    use_resampler: bool = True,
    height: int = 600,
) -> go.Figure:
    """去趋势后的 orderbook 散点图（价格 - wall_mid）。"""
    p = prices_long.filter(pl.col("product") == product)
    wm = wall_mid_df.filter(pl.col("product") == product)
    if day is not None:
        p = p.filter(pl.col("day") == day)
        wm = wm.filter(pl.col("day") == day)
    if filter_by_volume and min_order_volume is not None and "volume" in p.columns:
        p = p.filter(pl.col("volume") >= min_order_volume)

    # Join wall_mid to prices
    joined = p.join(wm.select(["timestamp", "product", "wall_mid"]), on=["timestamp", "product"], how="left")
    joined = joined.with_columns((pl.col("price") - pl.col("wall_mid")).alias("norm_price"))

    if overlay_wall_mid_trend and overlay_raw_wall_mid:
        fig = _wrap_with_resampler(make_subplots(specs=[[{"secondary_y": True}]]), use_resampler=use_resampler)
    else:
        fig = _new_figure(use_resampler=use_resampler)
    max_vol = joined["volume"].max() or 1

    bids = joined.filter(pl.col("side") == "bid")
    fig.add_trace(go.Scatter(
        x=bids["timestamp"].to_list(),
        y=bids["norm_price"].to_list(),
        mode="markers",
        marker=dict(color="steelblue", size=[v / max_vol * 15 for v in bids["volume"].to_list()], opacity=0.5),
        customdata=bids["volume"].to_list(),
        hovertemplate="t=%{x}<br>p=%{y}<br>v=%{customdata}<extra></extra>",
        name="Bid",
    ))

    asks = joined.filter(pl.col("side") == "ask")
    fig.add_trace(go.Scatter(
        x=asks["timestamp"].to_list(),
        y=asks["norm_price"].to_list(),
        mode="markers",
        marker=dict(color="salmon", size=[v / max_vol * 15 for v in asks["volume"].to_list()], opacity=0.5),
        customdata=asks["volume"].to_list(),
        hovertemplate="t=%{x}<br>p=%{y}<br>v=%{customdata}<extra></extra>",
        name="Ask",
    ))

    irrational_orders = joined.filter(
        ((pl.col("side") == "bid") & (pl.col("norm_price") > 0))
        | ((pl.col("side") == "ask") & (pl.col("norm_price") < 0))
    )
    if irrational_orders.height > 0:
        irr_sizes = [
            max(10, min(28, abs(v) / max_vol * 30)) if v is not None else 10
            for v in irrational_orders["volume"].to_list()
        ]
        fig.add_trace(go.Scatter(
            x=irrational_orders["timestamp"].to_list(),
            y=irrational_orders["norm_price"].to_list(),
            mode="markers",
            marker=dict(
                color="gold",
                symbol="x",
                size=irr_sizes,
                line=dict(width=1, color="black"),
            ),
            name="Cross-Fair Orders",
            customdata=list(zip(irrational_orders["price"].to_list(), irrational_orders["volume"].to_list())),
            hovertemplate="t=%{x}<br>p=%{y}<br>px=%{customdata[0]}<br>qty=%{customdata[1]}<extra>Cross-Fair</extra>",
        ))

    near_fair_orders = joined.filter(
        (pl.col("norm_price").abs() <= 4)
        & (
            ((pl.col("side") == "bid") & (pl.col("norm_price") <= 0))
            | ((pl.col("side") == "ask") & (pl.col("norm_price") >= 0))
        )
    )
    if near_fair_orders.height > 0:
        near_sizes = [
            max(8, min(20, abs(v) / max_vol * 18)) if v is not None else 8
            for v in near_fair_orders["volume"].to_list()
        ]
        fig.add_trace(go.Scatter(
            x=near_fair_orders["timestamp"].to_list(),
            y=near_fair_orders["norm_price"].to_list(),
            mode="markers",
            marker=dict(
                color="purple",
                symbol="diamond",
                size=near_sizes,
                line=dict(width=1, color="indigo"),
                opacity=0.9,
            ),
            name="Near-Fair Rational Orders (|Δ|<=4)",
            customdata=list(zip(near_fair_orders["price"].to_list(), near_fair_orders["volume"].to_list())),
            hovertemplate="t=%{x}<br>p=%{y}<br>px=%{customdata[0]}<br>qty=%{customdata[1]}<extra>Near-Fair</extra>",
        ))

    # Trades mapped to normalized axis with the same wall_mid reference.
    if trades is not None:
        t = trades.filter(pl.col("product") == product)
        if day is not None and "day" in t.columns:
            t = t.filter(pl.col("day") == day)
        if filter_by_volume and min_trade_quantity is not None:
            trade_qty_col = "quantity" if "quantity" in t.columns else ("volume" if "volume" in t.columns else None)
            if trade_qty_col is not None:
                t = t.filter(pl.col(trade_qty_col).abs() >= min_trade_quantity)
        t_joined = t.join(wm.select(["timestamp", "product", "wall_mid"]), on=["timestamp", "product"], how="left")
        t_joined = t_joined.filter(pl.col("wall_mid").is_not_null())
        t_joined = t_joined.with_columns((pl.col("price") - pl.col("wall_mid")).alias("norm_price"))

        trade_qty_col = "quantity" if "quantity" in t_joined.columns else ("volume" if "volume" in t_joined.columns else None)
        if trade_qty_col is not None and t_joined.height > 0:
            q_abs = t_joined[trade_qty_col].abs()
            trade_max = q_abs.max() or 1
            trade_sizes = [4 + (v / trade_max) * 12 for v in q_abs.to_list()]
            trade_qty = t_joined[trade_qty_col].to_list()
        else:
            trade_sizes = 8
            trade_qty = [None] * t_joined.height

        fig.add_trace(go.Scatter(
            x=t_joined["timestamp"].to_list(),
            y=t_joined["norm_price"].to_list(),
            mode="markers",
            marker=dict(color="black", symbol="x", size=trade_sizes),
            customdata=trade_qty,
            hovertemplate="t=%{x}<br>p=%{y}<br>q=%{customdata}<extra></extra>",
            name="Trd",
        ))

    if overlay_wall_mid_trend and wm.height > 0:
        wm_trace = go.Scatter(
            x=wm["timestamp"].to_list(),
            y=wm["wall_mid"].to_list() if overlay_raw_wall_mid else [0.0] * wm.height,
            mode="lines",
            line=dict(color="green", width=1.2),
            name="WM",
            hovertemplate="t=%{x}<br>wm=%{y}<extra></extra>",
        )
        if overlay_raw_wall_mid:
            fig.add_trace(wm_trace, secondary_y=True)
            fig.update_yaxes(title_text="Wall Mid", secondary_y=True)
        else:
            fig.add_trace(wm_trace)

    fig.add_hline(y=0, line_color="green", line_dash="dash", annotation_text="Fair")
    title = f"{product} Normalized" + (f" Day {day}" if day is not None else "")
    fig.update_layout(title=title, height=height, xaxis_title="Timestamp", yaxis_title="Distance from Fair (ticks)")
    fig.update_layout(legend=dict(font=dict(size=10), orientation="h", y=1.02, x=0))
    return fig
