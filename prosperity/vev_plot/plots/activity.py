"""成交活跃度 heatmap：rows = product, cols = time bucket, value = trade count (or qty)。

用于快速定位哪条 voucher / 哪段时间有流动性。
"""
from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import polars as pl

from ..context import Context


def plot_activity(
    ctx: Context,
    *,
    time_bin: int = 20_000,
    metric: str = "count",
    height: int = 500,
) -> go.Figure:
    if ctx.trades.is_empty():
        return go.Figure()

    trades = ctx.trades.with_columns(
        ((pl.col("global_ts") // time_bin) * time_bin).alias("ts_bin")
    )

    if metric == "count":
        agg = trades.group_by(["product", "ts_bin"]).len().rename({"len": "val"})
    else:
        agg = trades.group_by(["product", "ts_bin"]).agg(
            pl.col("quantity").sum().alias("val")
        )

    # 按 strike 顺序排列产品
    product_order = (
        [f"VEV_{k}" for k in [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]]
        + ["VELVETFRUIT_EXTRACT", "HYDROGEL_PACK"]
    )
    products_present = [p for p in product_order if p in agg["product"].unique().to_list()]

    pivot = (
        agg.pivot(index="product", on="ts_bin", values="val")
        .with_columns(pl.col("product").cast(pl.Categorical))
    )

    import numpy as np
    ts_cols = [c for c in pivot.columns if c != "product"]
    ts_cols_sorted = sorted(ts_cols, key=lambda x: int(x))
    z = (
        pivot.select(["product", *ts_cols_sorted])
        .filter(pl.col("product").is_in(products_present))
    )
    # reorder rows to product_order
    z_dict = {row["product"]: [row[c] for c in ts_cols_sorted] for row in z.iter_rows(named=True)}
    matrix = np.array([z_dict.get(p, [None] * len(ts_cols_sorted)) for p in products_present], dtype=float)

    fig = go.Figure(
        go.Heatmap(
            z=matrix,
            x=[int(c) for c in ts_cols_sorted],
            y=products_present,
            colorscale="Viridis",
            hovertemplate=f"t_bin=%{{x}}<br>product=%{{y}}<br>{metric}=%{{z}}<extra></extra>",
        )
    )
    for d in ctx.days[1:]:
        fig.add_vline(x=d * 1_000_000, line=dict(color="white", width=1, dash="dash"))

    fig.update_layout(
        height=height,
        title=f"Trade activity heatmap (bin = {time_bin} ts, metric = {metric})",
    )
    return fig
