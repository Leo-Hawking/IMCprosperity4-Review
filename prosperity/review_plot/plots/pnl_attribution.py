"""Step 7: 累计 edge PnL vs 总 PnL，揭示 MTM 分量。"""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import ReviewContext


def plot_pnl_attribution(ctx: ReviewContext) -> go.Figure:
    products = ctx.products
    rows = max(1, len(products))
    fig = make_subplots(
        rows=rows, cols=1,
        shared_xaxes=True,
        subplot_titles=[f"{p} — PnL Attribution" for p in products],
        vertical_spacing=0.08,
    )

    for i, product in enumerate(products, start=1):
        pt = ctx.my_with_fair.filter(pl.col("product") == product).sort("timestamp")
        pnl_p = ctx.pnl_by_product.filter(pl.col("product") == product).sort("timestamp")

        if not pt.is_empty():
            ts_list = pt["timestamp"].to_list()
            cum_edge = np.cumsum(
                [v if v is not None else 0.0 for v in pt["edge_pnl"].to_list()]
            )
            fig.add_trace(go.Scatter(
                x=ts_list,
                y=cum_edge.tolist(),
                mode="lines+markers",
                line=dict(color="green", width=1.5),
                marker=dict(size=4),
                name="Cum Edge PnL",
                legendgroup="edge",
                showlegend=(i == 1),
                hovertemplate="t=%{x}<br>cum_edge=%{y:.2f}<extra></extra>",
            ), row=i, col=1)
            final_edge = float(cum_edge[-1]) if len(cum_edge) else 0.0
        else:
            final_edge = 0.0

        if not pnl_p.is_empty():
            fig.add_trace(go.Scatter(
                x=pnl_p["timestamp"].to_list(),
                y=pnl_p["profit_and_loss"].to_list(),
                mode="lines",
                line=dict(color="orange", width=1.5),
                name="Total PnL",
                legendgroup="total",
                showlegend=(i == 1),
                hovertemplate="t=%{x}<br>total_pnl=%{y:.2f}<extra></extra>",
            ), row=i, col=1)
            final_total = float(pnl_p["profit_and_loss"].to_list()[-1])
        else:
            final_total = 0.0

        fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5, row=i, col=1)
        fig.update_yaxes(title_text="PnL", row=i, col=1)

        print(
            f"{product}: cum_edge={final_edge:.2f}, total_pnl={final_total:.2f}, "
            f"mtm_component={final_total - final_edge:.2f}"
        )

    fig.update_xaxes(title_text="Timestamp", row=rows, col=1)
    fig.update_layout(
        height=400 * rows, width=1200,
        legend=dict(orientation="h", y=1.03, x=0),
    )
    return fig
