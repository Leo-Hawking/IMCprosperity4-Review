"""PnL 分解图：单组合 + 多 group 对比。两种 mode：cash / edge。"""
from __future__ import annotations

from typing import Iterable, Optional

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..context import ReviewContext
from ..pnl_decomp import compute_decomp


_COLORS = {
    "total": "black",
    "realized": "#2ca02c",
    "mtm":      "#1f77b4",
    "edge":     "#2ca02c",
    "drift":    "#1f77b4",
}


def _components_for_mode(mode: str) -> tuple[str, str, str, str]:
    """Return (compA_col, compB_col, compA_label, compB_label) for the chosen mode."""
    if mode == "cash":
        return ("realized", "mtm", "realized cash", "MTM (position·fair)")
    if mode == "edge":
        return ("edge", "drift", "edge (做市质量)", "drift (持仓时段 fair 漂移)")
    raise ValueError(f"unknown mode: {mode!r}, expected 'cash' or 'edge'")


def _add_three_traces(fig, decomp, compA, compB, labelA, labelB,
                      row=None, col=None,
                      legendgroup: Optional[str] = None,
                      showlegend: bool = True) -> None:
    ts = decomp["timestamp"].to_numpy()
    common = dict(legendgroup=legendgroup, showlegend=showlegend, mode="lines")
    fig.add_trace(go.Scatter(
        x=ts, y=decomp["total"].to_numpy(),
        name="total",
        line=dict(color=_COLORS["total"], width=2),
        **common,
    ), row=row, col=col)
    fig.add_trace(go.Scatter(
        x=ts, y=decomp[compA].to_numpy(),
        name=labelA,
        line=dict(color=_COLORS[compA], width=1.4),
        **common,
    ), row=row, col=col)
    fig.add_trace(go.Scatter(
        x=ts, y=decomp[compB].to_numpy(),
        name=labelB,
        line=dict(color=_COLORS[compB], width=1.4),
        **common,
    ), row=row, col=col)


def plot_pnl_decomp(
    ctx: ReviewContext,
    products: Optional[Iterable[str]] = None,
    title: Optional[str] = None,
    fair_source: str = "mid",
    mode: str = "edge",
) -> go.Figure:
    """单组合 PnL 分解：total = compA + compB。

    products=None 时使用 ctx 里的全部产品。
    fair_source: 'mid' (默认) | 'round5_walmid' | callable
    mode: 'edge' (default) edge+drift | 'cash' realized+mtm
    """
    products = list(products) if products is not None else list(ctx.products)
    decomp = compute_decomp(ctx, products, fair_source=fair_source)
    compA, compB, labelA, labelB = _components_for_mode(mode)

    fig = go.Figure()
    _add_three_traces(fig, decomp, compA, compB, labelA, labelB)

    fig.add_hline(y=0, line=dict(color="gray", width=0.5, dash="dash"))
    if decomp.height:
        final = (
            f"final total={decomp['total'][-1]:,.0f}  "
            f"{labelA}={decomp[compA][-1]:,.0f}  "
            f"{labelB}={decomp[compB][-1]:,.0f}"
        )
    else:
        final = "(empty)"
    src_label = fair_source if isinstance(fair_source, str) else "custom"
    fig.update_layout(
        title=title or f"PnL decomp [mode={mode}, fair={src_label}] "
                       f"({len(products)} products) — {final}",
        xaxis_title="timestamp",
        yaxis_title="PnL",
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.18),
    )
    return fig


def plot_groups_decomp(
    ctx: ReviewContext,
    groups: dict[str, list[str]],
    cols: int = 2,
    height_per_row: int = 320,
    fair_source: str = "mid",
    mode: str = "edge",
) -> go.Figure:
    """多 group 多 panel 子图，每 group 一格 (total / compA / compB)。"""
    compA, compB, labelA, labelB = _components_for_mode(mode)
    names = list(groups.keys())
    n = len(names)
    rows = (n + cols - 1) // cols
    titles = []
    decomps = {}
    for name in names:
        d = compute_decomp(ctx, groups[name], fair_source=fair_source)
        decomps[name] = d
        if d.height:
            titles.append(f"{name}  (T={d['total'][-1]:,.0f}  "
                          f"A={d[compA][-1]:,.0f}  B={d[compB][-1]:,.0f})")
        else:
            titles.append(f"{name} (no data)")

    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=titles,
        shared_xaxes=False,
        vertical_spacing=0.06,
        horizontal_spacing=0.07,
    )

    for i, name in enumerate(names):
        r = i // cols + 1
        c = i % cols + 1
        d = decomps[name]
        if d.is_empty():
            continue
        _add_three_traces(
            fig, d, compA, compB, labelA, labelB,
            row=r, col=c,
            legendgroup="decomp",
            showlegend=(i == 0),
        )
        fig.add_hline(y=0, line=dict(color="gray", width=0.5, dash="dash"),
                      row=r, col=c)

    src_label = fair_source if isinstance(fair_source, str) else "custom"
    fig.update_layout(
        height=height_per_row * rows,
        title=f"PnL decomp by group [mode={mode}, fair={src_label}]",
        hovermode="x unified",
        legend=dict(orientation="h", y=1.02, x=0.5, xanchor="center"),
        margin=dict(t=80),
    )
    fig.update_xaxes(title_text="timestamp")
    fig.update_yaxes(title_text="PnL")
    return fig
