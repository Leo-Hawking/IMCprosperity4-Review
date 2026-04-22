"""Plotly 图像组装层 — 每个子模块只做 ctx -> go.Figure 一件事。"""
from .main_review import (
    plot_main_overview,
    plot_main_review,
    plot_main_zoom,
)
from .normalized_review import (
    plot_normalized_overview,
    plot_normalized_review,
    plot_normalized_zoom,
)
from .edge_scatter import plot_edge_scatter
from .fill_histogram import plot_fill_histogram
from .trade_interval import plot_trade_interval
from .pnl_attribution import plot_pnl_attribution
from .summary_table import build_summary

__all__ = [
    "plot_main_review",
    "plot_main_overview",
    "plot_main_zoom",
    "plot_normalized_review",
    "plot_normalized_overview",
    "plot_normalized_zoom",
    "plot_edge_scatter",
    "plot_fill_histogram",
    "plot_trade_interval",
    "plot_pnl_attribution",
    "build_summary",
]
