"""Step 5c: 成交 (price - fair) 分布直方图, buy / sell 叠加。"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import ReviewContext


def plot_fill_histogram(ctx: ReviewContext) -> go.Figure:
    products = ctx.products
    cols = max(1, len(products))
    fig = make_subplots(
        rows=1, cols=cols,
        subplot_titles=[f"{p} — Fill Level Distribution" for p in products],
    )

    for i, product in enumerate(products, start=1):
        pt = ctx.my_with_fair.filter(pl.col("product") == product)
        if pt.is_empty():
            fig.add_vline(x=0, line_dash="dash", line_color="black", line_width=1, row=1, col=i)
            fig.update_xaxes(title_text="Price - Fair", row=1, col=i)
            continue

        buys = pt.filter(pl.col("trade_type") == "my_buy").drop_nulls("delta")
        sells = pt.filter(pl.col("trade_type") == "my_sell").drop_nulls("delta")

        if not buys.is_empty():
            fig.add_trace(go.Histogram(
                x=buys["delta"].to_list(),
                marker_color="limegreen",
                opacity=0.7,
                name="Buy",
                legendgroup="buy",
                showlegend=(i == 1),
            ), row=1, col=i)
        if not sells.is_empty():
            fig.add_trace(go.Histogram(
                x=sells["delta"].to_list(),
                marker_color="red",
                opacity=0.7,
                name="Sell",
                legendgroup="sell",
                showlegend=(i == 1),
            ), row=1, col=i)

        fig.add_vline(x=0, line_dash="dash", line_color="black", line_width=1, row=1, col=i)
        fig.update_xaxes(title_text="Price - Fair", row=1, col=i)

    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_layout(
        height=400, width=1200,
        barmode="overlay",
        legend=dict(orientation="h", y=1.08, x=0),
    )
    return fig
