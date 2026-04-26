"""Moneyness 视图: 每个 day 一列，x=moneyness, y=voucher mid (绝对价格)。

注意：此图不计算 IV（那需要 fair value 和 BS 模型），只在 moneyness 空间展示
voucher mid 的分布形态。同一 strike 被不同时刻 underlying 拖动，moneyness 会随之漂移。
"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..markers import strike_color


def plot_moneyness(
    ctx: Context,
    *,
    sample_every: int = 50,
    height: int = 500,
) -> go.Figure:
    df = ctx.voucher_panel()
    if df.is_empty():
        return go.Figure()

    df = df.filter(pl.col("moneyness").is_not_null())
    if sample_every > 1:
        df = df.with_row_index("_idx").filter(pl.col("_idx") % sample_every == 0).drop("_idx")

    days = sorted(df["day"].unique().to_list())
    fig = make_subplots(
        rows=1, cols=len(days),
        subplot_titles=[f"day {d} (TTE start = {8 - d}d)" for d in days],
        horizontal_spacing=0.05,
    )

    for col_idx, d in enumerate(days):
        sub = df.filter(pl.col("day") == d)
        for k in sorted(sub["strike"].unique().to_list()):
            s = sub.filter(pl.col("strike") == k).sort("timestamp")
            fig.add_trace(
                go.Scatter(
                    x=s["moneyness"].to_list(),
                    y=s["mid_price"].to_list(),
                    mode="markers",
                    marker=dict(color=strike_color(int(k)), size=4, opacity=0.5),
                    name=f"VEV_{int(k)}",
                    legendgroup=f"VEV_{int(k)}",
                    showlegend=(col_idx == 0),
                    customdata=list(zip(s["timestamp"].to_list(), s["tte_days"].to_list())),
                    hovertemplate=(
                        f"<b>VEV_{int(k)}</b><br>"
                        "m_t=%{x:.3f}<br>voucher_mid=%{y:.2f}<br>"
                        "t=%{customdata[0]}<br>tte_days=%{customdata[1]:.2f}"
                        "<extra></extra>"
                    ),
                ),
                row=1, col=col_idx + 1,
            )
        fig.update_xaxes(title="m_t = ln(K/S) / √T", row=1, col=col_idx + 1)
        fig.update_yaxes(title="voucher mid", row=1, col=col_idx + 1)

    fig.update_layout(
        height=height,
        title="Voucher mid vs. moneyness —— 三日对比（若 smile 稳定，三图形状应一致）",
        hovermode="closest",
    )
    return fig
