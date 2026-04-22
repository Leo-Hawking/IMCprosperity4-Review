"""统一的 marker / trace 构造器。

所有散点 / 线条 marker 都要在 customdata 里带 (qty, px, delta)，my_buy / my_sell 另带
(edge, edge_pnl)。hover 模板由这里构造，不要在 plots/ 里复制粘贴。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import plotly.graph_objects as go
import polars as pl


HOVER_BASE = (
    "t=%{x}<br>y=%{y}<br>"
    "qty=%{customdata[0]}<br>px=%{customdata[1]}<br>Δ=%{customdata[2]}"
)
HOVER_WITH_EDGE = HOVER_BASE + "<br>edge=%{customdata[3]}<br>edge_pnl=%{customdata[4]}"


@dataclass
class MarkerSpec:
    name: str
    color: str
    symbol: str = "circle"
    size: int | float = 8
    line_color: Optional[str] = None
    line_width: float = 0
    opacity: float = 0.85
    size_by_volume: bool = False
    size_cap: int = 14
    size_floor: int = 2
    volume_power: float = 0.6
    with_edge: bool = False  # customdata 是否带 edge/edge_pnl
    legendgroup: str = ""
    extra_trace_kwargs: dict = field(default_factory=dict)


def _scale_sizes(volumes: list[float], cap: int, floor_: int, power: float) -> list[float]:
    if not volumes:
        return []
    abs_vals = [abs(float(v)) for v in volumes]
    sorted_vals = sorted(abs_vals)
    p95_idx = int((len(sorted_vals) - 1) * 0.95)
    ref_v = sorted_vals[p95_idx] or 1.0

    if cap <= floor_:
        return [float(cap)] * len(volumes)

    return [
        floor_ + (min(v / ref_v, 1.0) ** power) * (cap - floor_)
        for v in abs_vals
    ]


def _build_customdata(
    df: pl.DataFrame,
    x_col: str,
    y_col: str,
    qty_col: str,
    px_col: str,
    delta_col: str,
    with_edge: bool,
) -> list[list[Any]]:
    n = df.height
    if n == 0:
        return []
    qty = df[qty_col].to_list() if qty_col in df.columns else [None] * n
    px = df[px_col].to_list() if px_col in df.columns else [None] * n
    delta = (
        [round(v, 2) if v is not None else None for v in df[delta_col].to_list()]
        if delta_col in df.columns
        else [None] * n
    )
    if not with_edge:
        return [[q, p, d] for q, p, d in zip(qty, px, delta)]
    edge = (
        [round(v, 2) if v is not None else None for v in df["edge"].to_list()]
        if "edge" in df.columns
        else [None] * n
    )
    edge_pnl = (
        [round(v, 2) if v is not None else None for v in df["edge_pnl"].to_list()]
        if "edge_pnl" in df.columns
        else [None] * n
    )
    return [[q, p, d, e, ep] for q, p, d, e, ep in zip(qty, px, delta, edge, edge_pnl)]


def build_marker_trace(
    df: pl.DataFrame,
    spec: MarkerSpec,
    *,
    x_col: str = "timestamp",
    y_col: str = "price",
    qty_col: str = "quantity",
    px_col: str = "price",
    delta_col: str = "delta",
    volume_col: str = "volume",
    hover_suffix: str = "",
) -> Optional[go.Scatter]:
    if df.is_empty():
        return None

    x = df[x_col].to_list()
    y = df[y_col].to_list()

    if spec.size_by_volume and volume_col in df.columns:
        sizes = _scale_sizes(
            df[volume_col].to_list(),
            spec.size_cap,
            spec.size_floor,
            spec.volume_power,
        )
    else:
        sizes = spec.size

    marker = dict(
        color=spec.color,
        symbol=spec.symbol,
        size=sizes,
        opacity=spec.opacity,
    )
    if spec.line_color or spec.line_width:
        marker["line"] = dict(width=spec.line_width, color=spec.line_color or spec.color)

    cd = _build_customdata(df, x_col, y_col, qty_col, px_col, delta_col, spec.with_edge)
    hover = (HOVER_WITH_EDGE if spec.with_edge else HOVER_BASE) + hover_suffix + f"<extra>{spec.name}</extra>"

    return go.Scatter(
        x=x,
        y=y,
        mode="markers",
        marker=marker,
        name=spec.name,
        legendgroup=spec.legendgroup or spec.name,
        customdata=cd,
        hovertemplate=hover,
        **spec.extra_trace_kwargs,
    )


def build_line_trace(
    df: pl.DataFrame,
    *,
    x_col: str,
    y_col: str,
    name: str,
    color: str = "green",
    width: float = 1.5,
    dash: Optional[str] = None,
    shape: Optional[str] = None,
    legendgroup: str = "",
    hover: Optional[str] = None,
    yaxis: Optional[str] = None,
) -> Optional[go.Scatter]:
    if df.is_empty():
        return None
    line = dict(color=color, width=width)
    if dash:
        line["dash"] = dash
    if shape:
        line["shape"] = shape
    kwargs: dict = {}
    if yaxis:
        kwargs["yaxis"] = yaxis
    return go.Scatter(
        x=df[x_col].to_list(),
        y=df[y_col].to_list(),
        mode="lines",
        line=line,
        name=name,
        legendgroup=legendgroup or name,
        hovertemplate=hover or f"t=%{{x}}<br>{name}=%{{y}}<extra></extra>",
        **kwargs,
    )


# ========== 预制 spec ==========

BIDS = MarkerSpec(
    name="Bids", color="steelblue", symbol="circle",
    size_by_volume=True, size_cap=14, size_floor=2,
    opacity=0.4, legendgroup="ob",
)

ASKS = MarkerSpec(
    name="Asks", color="salmon", symbol="circle",
    size_by_volume=True, size_cap=14, size_floor=2,
    opacity=0.4, legendgroup="ob",
)

CROSS_FAIR = MarkerSpec(
    name="Cross-Fair", color="gold", symbol="x",
    size_by_volume=True, size_cap=10, size_floor=6,
    line_color="black", line_width=1, opacity=0.95,
    legendgroup="cross",
)

NEAR_FAIR = MarkerSpec(
    name="Near-Fair", color="purple", symbol="diamond",
    size_by_volume=True, size_cap=10, size_floor=5,
    line_color="indigo", line_width=1, opacity=0.9,
    legendgroup="near",
)

MKT_TRADES = MarkerSpec(
    name="Market Trades", color="black", symbol="x",
    size=7, opacity=0.85, legendgroup="mkt",
)

MY_BUY = MarkerSpec(
    name="My Buy", color="limegreen", symbol="triangle-up",
    size=10, size_by_volume=True, size_cap=13, size_floor=7, volume_power=0.6,
    line_color="darkgreen", line_width=1,
    opacity=0.95, legendgroup="my", with_edge=True,
)

MY_SELL = MarkerSpec(
    name="My Sell", color="red", symbol="triangle-down",
    size=10, size_by_volume=True, size_cap=13, size_floor=7, volume_power=0.6,
    line_color="darkred", line_width=1,
    opacity=0.95, legendgroup="my", with_edge=True,
)
