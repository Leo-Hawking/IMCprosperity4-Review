"""vev_plot — Round 3 "Gloves Off" 期权初探的绘图包。

典型用法:
    from vev_plot import Context
    from vev_plot.plots import (
        plot_overview, plot_voucher_grid, plot_spread,
        plot_moneyness, plot_strike_arb, plot_activity, plot_day_overlay,
    )
    from vev_plot.resample import enable_plotly_resample

    enable_plotly_resample(max_points=4000)
    ctx = Context.from_data_dir("data")
    plot_overview(ctx).show()

注: fair value 计算暂未落地 —— `vev_plot.fair` 默认返回 wall_mid，
为后续 IV / Δ 计算留好接口。
"""
from .context import Context
from .dataio import (
    ALL_PRODUCTS, DELTA1_EXTRA, UNDERLYING, VOUCHER_STRIKES, VOUCHER_SYMBOLS,
    load_days,
)
from .resample import disable_plotly_resample, enable_plotly_resample, no_resample

__all__ = [
    "Context",
    "load_days",
    "VOUCHER_STRIKES",
    "VOUCHER_SYMBOLS",
    "UNDERLYING",
    "DELTA1_EXTRA",
    "ALL_PRODUCTS",
    "enable_plotly_resample",
    "disable_plotly_resample",
    "no_resample",
]
