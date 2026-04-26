"""Strike-spread vs strike-diff：相邻 voucher 的无套利检查。

对两个 strike k1 < k2 的 call: price(k1) - price(k2) ∈ [0, k2 - k1]。
超出此区间即为无套利违规，可作为 free PnL 信号（受点差 / 仓位限制）。
"""
from __future__ import annotations

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..dataio import VOUCHER_STRIKES


def plot_strike_arb(
    ctx: Context,
    *,
    pairs: list[tuple[int, int]] | None = None,
    height: int = 900,
) -> go.Figure:
    if pairs is None:
        strikes = [k for k in VOUCHER_STRIKES if f"VEV_{k}" in ctx.products]
        pairs = list(zip(strikes[:-1], strikes[1:]))

    n = len(pairs)
    fig = make_subplots(
        rows=n, cols=1, shared_xaxes=True,
        subplot_titles=[f"VEV_{k1} - VEV_{k2}  (max allowed = {k2 - k1})" for (k1, k2) in pairs],
        vertical_spacing=0.03,
    )

    # pivot voucher panel: (day, timestamp, global_ts) x strike -> mid_price
    voucher_mid = (
        ctx.voucher_panel()
        .select(["day", "timestamp", "global_ts", "strike", "mid_price"])
        .filter(pl.col("strike").is_not_null())
        .pivot(index=["day", "timestamp", "global_ts"], on="strike", values="mid_price")
        .sort("global_ts")
    )

    for i, (k1, k2) in enumerate(pairs):
        cols_ok = [str(k1) in voucher_mid.columns, str(k2) in voucher_mid.columns]
        if not all(cols_ok):
            continue
        diff = voucher_mid.select([
            "global_ts",
            (pl.col(str(k1)) - pl.col(str(k2))).alias("price_diff"),
        ]).drop_nulls()

        fig.add_trace(
            go.Scatter(
                x=diff["global_ts"].to_list(),
                y=diff["price_diff"].to_list(),
                mode="lines", line=dict(width=1),
                name=f"VEV_{k1} - VEV_{k2}",
                hovertemplate="t=%{x}<br>price_diff=%{y:.2f}<extra></extra>",
                showlegend=False,
            ),
            row=i + 1, col=1,
        )
        # bound lines: 0 and k2-k1
        fig.add_hline(y=0, line=dict(color="rgba(0,0,0,0.4)", dash="dot"),
                      row=i + 1, col=1)
        fig.add_hline(y=k2 - k1, line=dict(color="crimson", dash="dot"),
                      row=i + 1, col=1)

        for d in ctx.days[1:]:
            fig.add_vline(x=d * 1_000_000, line=dict(color="rgba(0,0,0,0.3)", dash="dash"),
                          row=i + 1, col=1)

    fig.update_layout(
        height=height,
        title="No-arbitrage 检查：相邻 strike 的 mid 差 (上界=红线, 下界=0)",
        hovermode="x unified",
    )
    return fig
