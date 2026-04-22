"""主复盘图 Orderbook + Position + PnL。"""
from __future__ import annotations

from typing import Optional

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from .. import markers
from ..context import ReviewContext
from ._common import clip_ts


def plot_main_review(
    ctx: ReviewContext,
    product: str,
    *,
    ts_range: tuple[Optional[int], Optional[int]] = (None, None),
    full_resolution: bool = False,
    show_cross_fair: bool = True,
    show_near_fair: bool = True,
    show_spread_band: bool = True,
    show_drawdown: bool = True,
    show_position_caps: bool = True,
    show_spikes: bool = True,
    highlight_ranges: list[tuple[int, int]] | None = None,
    annotations: list[tuple[int, str]] | None = None,
) -> go.Figure:
    """三行共享横轴: Row1 Orderbook + Trades, Row2 Position, Row3 PnL。"""
    # full_resolution 由 notebook 侧用 no_resample() 包裹调用。
    _ = full_resolution  # 入参保留以保持 API 一致

    ob = clip_ts(ctx.ob_long.filter(pl.col("product") == product), ts_range)
    fair = clip_ts(
        ctx.fair_df.filter(pl.col("product") == product).sort("timestamp"), ts_range
    )
    mkt = clip_ts(ctx.mkt_trades.filter(pl.col("product") == product), ts_range)
    my_p = clip_ts(ctx.my_with_fair.filter(pl.col("product") == product), ts_range)
    pos = clip_ts(ctx.position_df.filter(pl.col("product") == product).sort("timestamp"), ts_range)
    pnl = clip_ts(ctx.pnl_by_product.filter(pl.col("product") == product).sort("timestamp"), ts_range)
    ob_wide_p = clip_ts(ctx.ob_wide.filter(pl.col("product") == product).sort("timestamp"), ts_range)

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.55, 0.2, 0.25],
        subplot_titles=[
            f"{product} — Orderbook + Trades",
            "Position",
            "PnL",
        ],
    )

    # ---- Row 1: spread band (A) ----
    if show_spread_band and not ob_wide_p.is_empty():
        _add_spread_band(fig, ob_wide_p, normalized=False, fair_df=fair, row=1)

    # ---- Row 1: Bids / Asks ----
    if ctx.cross_fair_only and "cross_fair" in ob.columns:
        bids_df = ob.filter((pl.col("side") == "bid") & pl.col("cross_fair"))
        asks_df = ob.filter((pl.col("side") == "ask") & pl.col("cross_fair"))
    else:
        bids_df = ob.filter(pl.col("side") == "bid")
        asks_df = ob.filter(pl.col("side") == "ask")

    _add_if(fig, markers.build_marker_trace(
        bids_df, markers.BIDS,
        y_col="price", qty_col="volume", px_col="price", delta_col="delta",
    ), row=1)
    _add_if(fig, markers.build_marker_trace(
        asks_df, markers.ASKS,
        y_col="price", qty_col="volume", px_col="price", delta_col="delta",
    ), row=1)

    # ---- Row 1: Cross-Fair markers ----
    if show_cross_fair and "cross_fair" in ob.columns:
        cross = ob.filter(pl.col("cross_fair"))
        _add_if(fig, markers.build_marker_trace(
            cross, markers.CROSS_FAIR,
            y_col="price", qty_col="volume", px_col="price", delta_col="delta",
        ), row=1)

    # ---- Row 1: Near-Fair markers ----
    if show_near_fair and "near_fair" in ob.columns and ctx.near_fair_tick is not None:
        near = ob.filter(pl.col("near_fair"))
        _add_if(fig, markers.build_marker_trace(
            near, markers.NEAR_FAIR,
            y_col="price", qty_col="volume", px_col="price", delta_col="delta",
        ), row=1)

    # ---- Row 1: Fair line ----
    fair_line = markers.build_line_trace(
        fair, x_col="timestamp", y_col="fair", name="Fair", color="green", width=1.5
    )
    _add_if(fig, fair_line, row=1)

    # ---- Row 1: Market trades ----
    _add_if(fig, markers.build_marker_trace(
        mkt, markers.MKT_TRADES,
        y_col="price", qty_col="quantity", px_col="price", delta_col="delta",
    ), row=1)

    # ---- Row 1: My buy / sell ----
    my_buy = my_p.filter(pl.col("trade_type") == "my_buy")
    my_sell = my_p.filter(pl.col("trade_type") == "my_sell")
    _add_if(fig, markers.build_marker_trace(
        my_buy, markers.MY_BUY,
        y_col="price", qty_col="quantity", px_col="price", delta_col="delta", volume_col="quantity",
    ), row=1)
    _add_if(fig, markers.build_marker_trace(
        my_sell, markers.MY_SELL,
        y_col="price", qty_col="quantity", px_col="price", delta_col="delta", volume_col="quantity",
    ), row=1)

    # ---- Row 2: Position ----
    pos_tr = markers.build_line_trace(
        pos, x_col="timestamp", y_col="position",
        name="Position", color="purple", width=1.5, shape="hv",
    )
    _add_if(fig, pos_tr, row=2)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=2, col=1)

    if show_position_caps and product in ctx.position_limits:
        limit = ctx.position_limits[product]
        fig.add_hline(y=limit, line_dash="dot", line_color="red", line_width=0.8, row=2, col=1)
        fig.add_hline(y=-limit, line_dash="dot", line_color="red", line_width=0.8, row=2, col=1)

    # ---- Row 3: PnL ----
    pnl_tr = markers.build_line_trace(
        pnl, x_col="timestamp", y_col="profit_and_loss",
        name=f"PnL ({product})", color="orange", width=1.5,
    )
    _add_if(fig, pnl_tr, row=3)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=3, col=1)

    if show_drawdown and not pnl.is_empty():
        _add_drawdown_shade(fig, pnl, row=3)

    # ---- Layout ----
    fig.update_layout(
        height=900, width=1200,
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=10)),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Timestamp", row=3, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Pos", row=2, col=1)
    fig.update_yaxes(title_text="PnL", row=3, col=1)

    if show_spikes:
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikethickness=1)

    if highlight_ranges:
        for a, b in highlight_ranges:
            fig.add_vrect(x0=a, x1=b, fillcolor="lightgrey", opacity=0.25, layer="below", line_width=0, row=1, col=1)

    if annotations:
        for ts_, text in annotations:
            fig.add_vline(x=ts_, line_dash="dot", line_color="slategray", line_width=1, row=1, col=1,
                          annotation_text=text, annotation_position="top")

    return fig


def plot_main_overview(ctx: ReviewContext, product: str, **opts) -> go.Figure:
    """全区间 + resample (调用方需先 enable_plotly_resample)。"""
    return plot_main_review(ctx, product, ts_range=(None, None), full_resolution=False, **opts)


def plot_main_zoom(
    ctx: ReviewContext,
    product: str,
    ts_range: tuple[int, int],
    **opts,
) -> go.Figure:
    """zoom 到 ts_range，禁用 resample 保留全精度。"""
    return plot_main_review(ctx, product, ts_range=ts_range, full_resolution=True, **opts)


# ---------- helpers ----------

def _add_if(fig: go.Figure, trace, row: int) -> None:
    if trace is not None:
        fig.add_trace(trace, row=row, col=1)


def _add_spread_band(
    fig: go.Figure,
    ob_wide_p: pl.DataFrame,
    normalized: bool,
    fair_df: pl.DataFrame,
    row: int,
) -> None:
    """Row1 填充 bid_1 / ask_1 之间的 spread 区域。"""
    if ob_wide_p.is_empty() or "bid_price_1" not in ob_wide_p.columns:
        return
    data = ob_wide_p.select(
        ["timestamp", "bid_price_1", "ask_price_1"]
    ).drop_nulls()
    if data.is_empty():
        return

    if normalized:
        joined = data.join(fair_df.select(["timestamp", "fair"]), on="timestamp", how="left")
        if joined.is_empty():
            return
        top_y = (joined["ask_price_1"] - joined["fair"]).to_list()
        bot_y = (joined["bid_price_1"] - joined["fair"]).to_list()
        x = joined["timestamp"].to_list()
    else:
        x = data["timestamp"].to_list()
        top_y = data["ask_price_1"].to_list()
        bot_y = data["bid_price_1"].to_list()

    fig.add_trace(go.Scatter(
        x=x, y=bot_y, mode="lines",
        line=dict(color="rgba(0,0,0,0)"),
        showlegend=False, hoverinfo="skip",
        name="_spread_bot",
    ), row=row, col=1)
    fig.add_trace(go.Scatter(
        x=x, y=top_y, mode="lines",
        line=dict(color="rgba(0,0,0,0)"),
        fill="tonexty", fillcolor="rgba(120,120,120,0.12)",
        showlegend=False, hoverinfo="skip",
        name="Spread Band",
    ), row=row, col=1)


def _add_drawdown_shade(fig: go.Figure, pnl: pl.DataFrame, row: int) -> None:
    ts = pnl["timestamp"].to_list()
    p = pnl["profit_and_loss"].to_list()
    if not p:
        return
    running_max = []
    mx = p[0]
    for v in p:
        if v is None:
            running_max.append(mx)
            continue
        mx = max(mx, v)
        running_max.append(mx)
    dd = [vi - m if vi is not None else None for vi, m in zip(p, running_max)]
    # 仅在 dd<0 段落填充
    fig.add_trace(go.Scatter(
        x=ts, y=dd,
        fill="tozeroy", fillcolor="rgba(220,50,50,0.12)",
        line=dict(color="rgba(0,0,0,0)"),
        name="Drawdown", showlegend=False, hoverinfo="skip",
    ), row=row, col=1, secondary_y=False)
