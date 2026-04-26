"""Trace builders —— 所有 plotly trace 由此构造，便于统一 hover 模板、配色。

命名规则:
    line_trace(df, x, y, name, color)          -> go.Scatter (lines)
    scatter_trace(df, x, y, name, color)       -> go.Scatter (markers)
    strike_color(strike)                        -> str  (稳定配色)
"""
from __future__ import annotations

from typing import Iterable

import plotly.colors as pc
import plotly.graph_objects as go
import polars as pl

from .dataio import VOUCHER_STRIKES

HOVER_BASE = (
    "t=%{x}<br>y=%{y:.4f}"
)


def _turbo_color(idx: int, n: int) -> str:
    if n <= 1:
        return pc.sample_colorscale("Turbo", [0.5])[0]
    return pc.sample_colorscale("Turbo", [idx / max(1, n - 1)])[0]


_STRIKE_COLORS: dict[int, str] = {
    k: _turbo_color(i, len(VOUCHER_STRIKES))
    for i, k in enumerate(VOUCHER_STRIKES)
}


def strike_color(strike: int) -> str:
    return _STRIKE_COLORS.get(int(strike), "#888")


def day_dash(day: int) -> str:
    return {0: "solid", 1: "dash", 2: "dot"}.get(int(day), "solid")


def _customdata_from(df: pl.DataFrame, cols: Iterable[str]) -> list[list]:
    out: list[list] = []
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return [[None] for _ in range(df.height)]
    values = [df[c].to_list() for c in cols]
    for row in zip(*values):
        out.append(list(row))
    return out


def line_trace(
    df: pl.DataFrame,
    *,
    x: str,
    y: str,
    name: str,
    color: str = "steelblue",
    dash: str = "solid",
    width: float = 1.3,
    yaxis: str | None = None,
    hover_cols: Iterable[str] = (),
    hover_title: str | None = None,
) -> go.Scatter | None:
    if df.is_empty() or x not in df.columns or y not in df.columns:
        return None
    cd = _customdata_from(df, hover_cols)
    extras = "".join(f"<br>{c}=%{{customdata[{i}]}}" for i, c in enumerate(hover_cols) if c in df.columns)
    tpl = f"<b>{hover_title or name}</b><br>" + HOVER_BASE + extras + "<extra></extra>"
    kwargs = {"yaxis": yaxis} if yaxis else {}
    return go.Scatter(
        x=df[x].to_list(),
        y=df[y].to_list(),
        mode="lines",
        line=dict(color=color, width=width, dash=dash),
        name=name,
        legendgroup=name,
        customdata=cd if cd else None,
        hovertemplate=tpl,
        **kwargs,
    )


def scatter_trace(
    df: pl.DataFrame,
    *,
    x: str,
    y: str,
    name: str,
    color: str = "steelblue",
    symbol: str = "circle",
    size: int | list[int] = 6,
    opacity: float = 0.7,
    hover_cols: Iterable[str] = (),
) -> go.Scatter | None:
    if df.is_empty() or x not in df.columns or y not in df.columns:
        return None
    cd = _customdata_from(df, hover_cols)
    extras = "".join(f"<br>{c}=%{{customdata[{i}]}}" for i, c in enumerate(hover_cols) if c in df.columns)
    tpl = f"<b>{name}</b><br>" + HOVER_BASE + extras + "<extra></extra>"
    return go.Scatter(
        x=df[x].to_list(),
        y=df[y].to_list(),
        mode="markers",
        marker=dict(color=color, symbol=symbol, size=size, opacity=opacity,
                    line=dict(width=0.3, color="rgba(0,0,0,0.4)")),
        name=name,
        legendgroup=name,
        customdata=cd if cd else None,
        hovertemplate=tpl,
    )


def day_shading(fig: go.Figure, days: list[int], ticks_per_day: int = 1_000_000):
    """在图上为每天画淡色背景，方便识别 day-0/1/2 的分界。"""
    palette = ["rgba(100,150,255,0.05)", "rgba(150,255,150,0.05)", "rgba(255,180,140,0.05)"]
    for i, d in enumerate(days):
        fig.add_vrect(
            x0=d * ticks_per_day,
            x1=(d + 1) * ticks_per_day,
            fillcolor=palette[i % len(palette)],
            opacity=1.0,
            line_width=0,
            layer="below",
        )
    for d in days[1:]:
        fig.add_vline(
            x=d * ticks_per_day,
            line=dict(color="rgba(0,0,0,0.3)", width=1, dash="dash"),
        )
