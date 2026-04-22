"""review_plot — Prosperity 提交 / 回测复盘绘图包。

典型用法:
    from review_plot import ReviewContext, load_submission
    from review_plot.plots import (
        plot_main_overview, plot_main_zoom,
        plot_normalized_overview, plot_normalized_zoom,
        plot_edge_scatter, plot_fill_histogram,
        plot_trade_interval, plot_pnl_attribution,
        build_summary,
    )
    from review_plot.resample import enable_plotly_resample
"""
from .context import ReviewContext, ReviewSlice
from .dataio import RawSubmission, load_submission
from .resample import (
    disable_plotly_resample,
    enable_plotly_resample,
    no_resample,
)

__all__ = [
    "ReviewContext",
    "ReviewSlice",
    "RawSubmission",
    "load_submission",
    "enable_plotly_resample",
    "disable_plotly_resample",
    "no_resample",
]
