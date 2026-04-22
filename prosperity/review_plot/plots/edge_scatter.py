"""Step 5b: 每笔我方成交的 edge 随时间散点。"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import ReviewContext


def plot_edge_scatter(ctx: ReviewContext) -> go.Figure:
    products = ctx.products
    rows = max(1, len(products))
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        subplot_titles=[f"{p} — Edge per Trade" for p in products],
        vertical_spacing=0.08,
    )

    for i, product in enumerate(products, start=1):
        pt = ctx.my_with_fair.filter(pl.col("product") == product).sort("timestamp")
        if pt.is_empty():
            fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=i, col=1)
            fig.update_yaxes(title_text="Edge", row=i, col=1)
            continue

        edges = pt["edge"].to_list()
        colors = [
            "green" if (e is not None and e > 0)
            else ("gray" if (e is not None and e == 0) else "red")
            for e in edges
        ]

        cd = list(zip(
            pt["quantity"].to_list(),
            pt["price"].to_list(),
            [round(v, 2) if v is not None else None for v in pt["delta"].to_list()],
            pt["trade_type"].to_list(),
            [round(v, 2) if v is not None else None for v in edges],
        ))
        hover = (
            "t=%{x}<br>edge=%{y}<br>"
            "qty=%{customdata[0]}<br>px=%{customdata[1]}<br>Δ=%{customdata[2]}<br>"
            "%{customdata[3]}<extra></extra>"
        )

        fig.add_trace(go.Scatter(
            x=pt["timestamp"].to_list(),
            y=edges,
            mode="markers",
            marker=dict(color=colors, size=8, line=dict(width=0.5, color="black")),
            customdata=cd,
            hovertemplate=hover,
            name=product,
            showlegend=False,
        ), row=i, col=1)
        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=i, col=1)
        fig.update_yaxes(title_text="Edge", row=i, col=1)

    fig.update_xaxes(title_text="Timestamp", row=rows, col=1)
    fig.update_layout(height=350 * rows, width=1200)
    return fig
