"""把 3 天的 underlying + voucher mid 叠到 "day-local" 时间轴上，看日内形态是否重复。"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..dataio import UNDERLYING, VOUCHER_STRIKES
from ..markers import day_dash, strike_color


def plot_day_overlay(
    ctx: Context,
    *,
    products: list[str] | None = None,
    normalize: bool = True,
    height: int = 600,
) -> go.Figure:
    if products is None:
        products = [UNDERLYING] + [f"VEV_{k}" for k in [5000, 5200, 5400]]

    n = len(products)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=True,
        subplot_titles=products,
        vertical_spacing=0.04,
    )

    for i, prod in enumerate(products):
        df = ctx.product_slice(prod)
        if df.is_empty():
            continue

        color = strike_color(int(prod.split("_")[-1])) if prod.startswith("VEV_") else "black"

        for d in ctx.days:
            sub = df.filter(pl.col("day") == d).sort("timestamp")
            if sub.is_empty():
                continue
            y = sub["mid_price"].to_list()
            if normalize and y:
                base = y[0] or 1
                y = [(v - base) for v in y]
            fig.add_trace(
                go.Scatter(
                    x=sub["timestamp"].to_list(),
                    y=y,
                    mode="lines", line=dict(color=color, width=1.2, dash=day_dash(d)),
                    name=f"{prod} d{d}",
                    legendgroup=f"d{d}",
                    hovertemplate=f"<b>{prod} day {d}</b><br>t=%{{x}}<br>y=%{{y:.2f}}<extra></extra>",
                    showlegend=(i == 0),
                ),
                row=i + 1, col=1,
            )

    title = "Day overlay (day-local t)  —  "
    title += "normalized (mid - mid_at_t0)" if normalize else "raw mid"
    fig.update_layout(
        height=height, title=title, hovermode="x unified",
    )
    return fig
