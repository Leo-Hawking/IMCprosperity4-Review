"""Step 6: 我方成交时间间隔直方图。"""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import ReviewContext


def plot_trade_interval(ctx: ReviewContext) -> go.Figure:
    products = ctx.products
    cols = max(1, len(products))
    fig = make_subplots(
        rows=1, cols=cols,
        subplot_titles=[f"{p} — Trade Interval (ms)" for p in products],
    )

    for i, product in enumerate(products, start=1):
        pt = ctx.my_trades.filter(pl.col("product") == product).sort("timestamp")
        if pt.height < 2:
            fig.update_xaxes(title_text="Interval (ms)", row=1, col=i)
            continue

        intervals = pt["timestamp"].diff().drop_nulls().to_list()

        fig.add_trace(go.Histogram(
            x=intervals,
            nbinsx=40,
            marker_color="steelblue",
            name=product,
            showlegend=False,
        ), row=1, col=i)

        fig.update_xaxes(title_text="Interval (ms)", row=1, col=i)

        arr = np.asarray(intervals)
        ts_list = pt["timestamp"].to_list()
        max_idx = int(np.argmax(arr)) + 1 if arr.size else 0
        max_gap_at = ts_list[max_idx] if 0 <= max_idx < len(ts_list) else None
        print(
            f"{product}: {len(intervals)} intervals, "
            f"mean={arr.mean():.0f}ms, median={np.median(arr):.0f}ms, "
            f"max={int(arr.max())}ms, max_gap_at_ts={max_gap_at}"
        )

    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_layout(height=350, width=1200)
    return fig
