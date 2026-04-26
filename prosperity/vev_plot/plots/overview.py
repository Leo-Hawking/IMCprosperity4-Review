"""Overview: underlying + 10 个 voucher 的 mid_price 合图，双 y 轴。

竖直 crosshair + hover unified，3 天连续，用 vrect 区分 day-0/1/2。
"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl

from ..context import Context
from ..dataio import UNDERLYING, VOUCHER_STRIKES
from ..markers import day_shading, line_trace, strike_color


def plot_overview(ctx: Context, *, height: int = 700) -> go.Figure:
    fig = go.Figure()

    und = ctx.product_slice(UNDERLYING)
    if not und.is_empty():
        fig.add_trace(
            line_trace(
                und,
                x="global_ts", y="mid_price",
                name=UNDERLYING, color="black", width=2.0,
                hover_cols=["day", "timestamp", "spread_1"],
            )
        )

    # voucher 归入右轴
    for k in VOUCHER_STRIKES:
        sym = f"VEV_{k}"
        df = ctx.product_slice(sym)
        if df.is_empty():
            continue
        fig.add_trace(
            line_trace(
                df,
                x="global_ts", y="mid_price",
                name=sym, color=strike_color(k), width=1.1,
                yaxis="y2",
                hover_cols=["day", "timestamp", "spread_1", "moneyness"],
            )
        )

    day_shading(fig, ctx.days)

    fig.update_layout(
        height=height,
        hovermode="x unified",
        title="Overview — VELVETFRUIT_EXTRACT (left) + 10 VEV vouchers (right)",
        xaxis=dict(
            title="global_ts (= day * 1M + timestamp)",
            showspikes=True, spikemode="across", spikesnap="cursor",
        ),
        yaxis=dict(title="underlying mid", side="left"),
        yaxis2=dict(title="voucher mid", side="right", overlaying="y", showgrid=False),
        legend=dict(orientation="h", y=-0.15),
    )
    return fig
