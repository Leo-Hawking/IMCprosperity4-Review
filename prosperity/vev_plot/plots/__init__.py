"""vev_plot.plots —— 所有绘图函数的入口。"""
from .activity import plot_activity
from .day_overlay import plot_day_overlay
from .depth import plot_depth_grid, plot_product_detail, plot_underlying_detail
from .iv_diagnostics import (
    plot_iv_smile,
    plot_iv_smile_3d,
    plot_iv_residual,
    plot_price_residual,
    plot_underlying_autocorr,
)
from .iv_surface import plot_iv_surface_overlay
from .moneyness import plot_moneyness
from .overview import plot_overview
from .spread import plot_spread
from .strike_arb import plot_strike_arb

__all__ = [
    "plot_product_detail",
    "plot_underlying_detail",
    "plot_depth_grid",
    "plot_overview",
    "plot_spread",
    "plot_moneyness",
    "plot_iv_smile",
    "plot_iv_smile_3d",
    "plot_iv_residual",
    "plot_price_residual",
    "plot_underlying_autocorr",
    "plot_iv_surface_overlay",
    "plot_strike_arb",
    "plot_activity",
    "plot_day_overlay",
]
