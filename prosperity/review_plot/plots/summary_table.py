"""Step 8: 汇总统计表, 返回 pl.DataFrame。"""
from __future__ import annotations

import polars as pl

from ..context import ReviewContext


def build_summary(ctx: ReviewContext) -> pl.DataFrame:
    rows = []
    for product in ctx.products:
        pt = ctx.my_with_fair.filter(pl.col("product") == product)
        pos_p = ctx.position_df.filter(pl.col("product") == product)
        pnl_p = ctx.pnl_by_product.filter(pl.col("product") == product).sort("timestamp")

        n_trades = pt.height
        total_vol = int(pt["quantity"].sum()) if n_trades else 0
        n_pos = pt.filter(pl.col("edge") > 0).height
        n_zero = pt.filter(pl.col("edge") == 0).height
        n_neg = pt.filter(pl.col("edge") < 0).height
        avg_edge = float(pt["edge"].mean()) if n_trades and pt["edge"].mean() is not None else 0.0
        cum_edge_pnl = float(pt["edge_pnl"].sum()) if n_trades and pt["edge_pnl"].sum() is not None else 0.0
        final_pnl = float(pnl_p["profit_and_loss"].to_list()[-1]) if pnl_p.height else 0.0
        max_pos = int(pos_p["position"].max()) if pos_p.height else 0
        min_pos = int(pos_p["position"].min()) if pos_p.height else 0
        final_pos = int(pos_p["position"].to_list()[-1]) if pos_p.height else 0

        if n_trades >= 2:
            ts_span = pt["timestamp"].max() - pt["timestamp"].min()
            freq = n_trades / (ts_span / 1000) if ts_span > 0 else 0.0
        else:
            freq = 0.0

        rows.append({
            "product": product,
            "trades": n_trades,
            "total_vol": total_vol,
            "pos_edge": n_pos,
            "zero_edge": n_zero,
            "neg_edge": n_neg,
            "avg_edge": round(avg_edge, 2),
            "cum_edge_pnl": round(cum_edge_pnl, 2),
            "final_pnl": round(final_pnl, 2),
            "mtm_component": round(final_pnl - cum_edge_pnl, 2),
            "max_pos": max_pos,
            "min_pos": min_pos,
            "final_pos": final_pos,
            "trades_per_1k_ts": round(freq, 2),
        })

    return pl.DataFrame(rows)
