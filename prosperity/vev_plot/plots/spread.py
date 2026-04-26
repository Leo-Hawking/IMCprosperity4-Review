"""Spread (ask-bid) 时序 + 分布。每个 voucher 一行，左边时序，右边直方图。"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..dataio import VOUCHER_STRIKES
from ..markers import strike_color


def plot_spread(ctx: Context, *, height: int = 1500) -> go.Figure:
    strikes = [k for k in VOUCHER_STRIKES if f"VEV_{k}" in ctx.products]
    n = len(strikes)

    fig = make_subplots(
        rows=n, cols=2,
        column_widths=[0.75, 0.25],
        shared_xaxes=False,
        subplot_titles=[s for k in strikes for s in (f"VEV_{k} spread over time", f"hist")],
        vertical_spacing=0.02, horizontal_spacing=0.05,
    )

    for i, k in enumerate(strikes):
        sym = f"VEV_{k}"
        df = ctx.product_slice(sym)
        if df.is_empty():
            continue
        color = strike_color(k)

        fig.add_trace(
            go.Scatter(
                x=df["global_ts"].to_list(),
                y=df["spread_1"].to_list(),
                mode="lines", line=dict(color=color, width=1),
                name=sym, showlegend=False,
                hovertemplate=f"<b>{sym}</b><br>t=%{{x}}<br>spread=%{{y}}<extra></extra>",
            ),
            row=i + 1, col=1,
        )

        fig.add_trace(
            go.Histogram(
                x=df["spread_1"].to_list(),
                marker_color=color, opacity=0.7,
                nbinsx=30, showlegend=False,
                hovertemplate=f"spread=%{{x}}<br>count=%{{y}}<extra>{sym}</extra>",
            ),
            row=i + 1, col=2,
        )

        for d in ctx.days[1:]:
            fig.add_vline(
                x=d * 1_000_000, line=dict(color="rgba(0,0,0,0.3)", width=1, dash="dash"),
                row=i + 1, col=1,
            )

    fig.update_layout(
        height=height, title="Per-voucher spread time-series and distribution",
        bargap=0.05,
    )
    return fig
