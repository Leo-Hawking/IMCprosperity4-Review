"""Normalized 主图: Row1 y = price - fair。"""
from __future__ import annotations

from typing import Optional

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from .. import markers
from ..context import ReviewContext
from ._common import clip_ts
from .main_review import _add_drawdown_shade, _add_if, _add_spread_band


def plot_normalized_review(
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
    show_fair_trend: bool = True,
    highlight_ranges: list[tuple[int, int]] | None = None,
    annotations: list[tuple[int, str]] | None = None,
) -> go.Figure:
    _ = full_resolution

    ob = clip_ts(ctx.ob_long.filter(pl.col("product") == product), ts_range)
    fair = clip_ts(
        ctx.fair_df.filter(pl.col("product") == product).sort("timestamp"), ts_range
    )
    mkt = clip_ts(ctx.mkt_trades.filter(pl.col("product") == product), ts_range)
    my_p = clip_ts(ctx.my_with_fair.filter(pl.col("product") == product), ts_range)
    pos = clip_ts(
        ctx.position_df.filter(pl.col("product") == product).sort("timestamp"), ts_range
    )
    pnl = clip_ts(
        ctx.pnl_by_product.filter(pl.col("product") == product).sort("timestamp"), ts_range
    )
    ob_wide_p = clip_ts(ctx.ob_wide.filter(pl.col("product") == product).sort("timestamp"), ts_range)

    # Row1 y = price - fair (delta)
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.7, 0.2, 0.25],
        subplot_titles=[
            f"{product} — Normalized (Δ from Fair)",
            "Position",
            "PnL",
        ],
    )

    # spread band on delta space
    if show_spread_band and not ob_wide_p.is_empty():
        _add_spread_band(fig, ob_wide_p, normalized=True, fair_df=fair, row=1)

    # Bids / Asks (y = delta)
    if ctx.cross_fair_only and "cross_fair" in ob.columns:
        bids_df = ob.filter((pl.col("side") == "bid") & pl.col("cross_fair"))
        asks_df = ob.filter((pl.col("side") == "ask") & pl.col("cross_fair"))
    else:
        bids_df = ob.filter(pl.col("side") == "bid")
        asks_df = ob.filter(pl.col("side") == "ask")

    _add_if(fig, markers.build_marker_trace(
        bids_df.drop_nulls("delta"), markers.BIDS,
        y_col="delta", qty_col="volume", px_col="price", delta_col="delta",
    ), row=1)
    _add_if(fig, markers.build_marker_trace(
        asks_df.drop_nulls("delta"), markers.ASKS,
        y_col="delta", qty_col="volume", px_col="price", delta_col="delta",
    ), row=1)

    if show_cross_fair and "cross_fair" in ob.columns:
        cross = ob.filter(pl.col("cross_fair")).drop_nulls("delta")
        _add_if(fig, markers.build_marker_trace(
            cross, markers.CROSS_FAIR,
            y_col="delta", qty_col="volume", px_col="price", delta_col="delta",
        ), row=1)

    if show_near_fair and "near_fair" in ob.columns and ctx.near_fair_tick is not None:
        near = ob.filter(pl.col("near_fair")).drop_nulls("delta")
        _add_if(fig, markers.build_marker_trace(
            near, markers.NEAR_FAIR,
            y_col="delta", qty_col="volume", px_col="price", delta_col="delta",
        ), row=1)

    # y=0 相当于 fair
    fig.add_hline(y=0, line_color="green", line_dash="dash", line_width=1, row=1, col=1,
                  annotation_text="Fair")

    _add_if(fig, markers.build_marker_trace(
        mkt.drop_nulls("delta"), markers.MKT_TRADES,
        y_col="delta", qty_col="quantity", px_col="price", delta_col="delta",
    ), row=1)

    my_buy = my_p.filter(pl.col("trade_type") == "my_buy").drop_nulls("delta")
    my_sell = my_p.filter(pl.col("trade_type") == "my_sell").drop_nulls("delta")
    _add_if(fig, markers.build_marker_trace(
        my_buy, markers.MY_BUY,
        y_col="delta", qty_col="quantity", px_col="price", delta_col="delta",
    ), row=1)
    _add_if(fig, markers.build_marker_trace(
        my_sell, markers.MY_SELL,
        y_col="delta", qty_col="quantity", px_col="price", delta_col="delta",
    ), row=1)

    # 副轴 fair trend
    if show_fair_trend and not fair.is_empty():
        fig.add_trace(go.Scatter(
            x=fair["timestamp"].to_list(),
            y=fair["fair"].to_list(),
            mode="lines",
            line=dict(color="green", width=1.2, dash="dot"),
            name="Fair Trend",
            legendgroup="fair",
            yaxis="y4",
            hovertemplate="t=%{x}<br>fair=%{y}<extra></extra>",
        ))

    # Row 2 / Row 3 同主图
    pos_tr = markers.build_line_trace(
        pos, x_col="timestamp", y_col="position",
        name="Position", color="purple", width=1.5, shape="hv",
    )
    _add_if(fig, pos_tr, row=2)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=2, col=1)
    if show_position_caps and product in ctx.position_limits:
        lim = ctx.position_limits[product]
        fig.add_hline(y=lim, line_dash="dot", line_color="red", line_width=0.8, row=2, col=1)
        fig.add_hline(y=-lim, line_dash="dot", line_color="red", line_width=0.8, row=2, col=1)

    pnl_tr = markers.build_line_trace(
        pnl, x_col="timestamp", y_col="profit_and_loss",
        name=f"PnL ({product})", color="orange", width=1.5,
    )
    _add_if(fig, pnl_tr, row=3)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=3, col=1)
    if show_drawdown and not pnl.is_empty():
        _add_drawdown_shade(fig, pnl, row=3)

    fig.update_layout(
        yaxis4=dict(overlaying="y", side="right", title="Fair", showgrid=False, anchor="x"),
        height=900, width=1200,
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=10)),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Timestamp", row=3, col=1)
    fig.update_yaxes(title_text="Δ from Fair (ticks)", row=1, col=1)
    fig.update_yaxes(title_text="Pos", row=2, col=1)
    fig.update_yaxes(title_text="PnL", row=3, col=1)

    if show_spikes:
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikethickness=1)

    if highlight_ranges:
        for a, b in highlight_ranges:
            fig.add_vrect(x0=a, x1=b, fillcolor="lightgrey", opacity=0.25,
                          layer="below", line_width=0, row=1, col=1)

    if annotations:
        for ts_, text in annotations:
            fig.add_vline(x=ts_, line_dash="dot", line_color="slategray", line_width=1,
                          row=1, col=1, annotation_text=text, annotation_position="top")

    return fig


def plot_normalized_overview(ctx: ReviewContext, product: str, **opts) -> go.Figure:
    return plot_normalized_review(ctx, product, ts_range=(None, None), full_resolution=False, **opts)


def plot_normalized_zoom(
    ctx: ReviewContext,
    product: str,
    ts_range: tuple[int, int],
    **opts,
) -> go.Figure:
    return plot_normalized_review(ctx, product, ts_range=ts_range, full_resolution=True, **opts)
