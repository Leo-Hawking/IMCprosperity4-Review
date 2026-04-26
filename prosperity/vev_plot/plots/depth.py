"""盘口深度 + 成交位置散点图。

支持两种模式：
  - normalize=True  : y = price - wall_mid，fair price 作为 wall_mid 的**累计变动**叠成虚线。
  - normalize=False : y = price (原尺度)，fair price = wall_mid 本身叠成虚线。

其他约定：
  - Bid = 绿, Ask = 红；bubble 比初版小一圈。
  - Trades 按相对同时刻 wall_mid 位置上色：
      price > wall_mid → 主动买 (紫)
      price < wall_mid → 主动卖 (黄)
"""
from __future__ import annotations

from typing import Iterable

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..dataio import UNDERLYING, VOUCHER_STRIKES


_LEVEL_STYLE = {
    1: dict(size_min=1.5, size_max=6.0, opacity=0.80),
    2: dict(size_min=1.0, size_max=4.5, opacity=0.45),
    3: dict(size_min=1.0, size_max=3.5, opacity=0.28),
}

_BUY_COLOR  = "#8e44ad"   # 紫：主动买
_SELL_COLOR = "#f1c40f"   # 黄：主动卖
_FAIR_COLOR = "rgba(30,30,30,0.75)"


def _size_from_volume(vol: list[float], lo: float, hi: float) -> list[float]:
    if not vol:
        return []
    abs_v = [abs(v) if v is not None else 0 for v in vol]
    sorted_v = sorted(abs_v)
    ref = sorted_v[int(len(sorted_v) * 0.95)] if sorted_v else 1.0
    ref = max(ref, 1.0)
    return [lo + min(v / ref, 1.0) * (hi - lo) for v in abs_v]


def _ob_long_with_wm(ctx: Context, product: str) -> pl.DataFrame:
    sub = ctx.ob_long.filter(pl.col("product") == product)
    if sub.is_empty():
        return sub
    wm = ctx.ob_wide.filter(pl.col("product") == product).select(
        ["day", "timestamp", "wall_mid"]
    )
    return sub.join(wm, on=["day", "timestamp"], how="left")


def _trades_with_wm(ctx: Context, product: str) -> pl.DataFrame:
    tr = ctx.trades.filter(pl.col("product") == product).sort(["day", "timestamp"])
    if tr.is_empty():
        return tr
    wm = (
        ctx.ob_wide.filter(pl.col("product") == product)
        .select(["day", "timestamp", "wall_mid"])
        .sort(["day", "timestamp"])
    )
    return tr.join_asof(wm, on="timestamp", by="day", strategy="backward")


# ---------------- Traces ----------------

def _book_trace(df: pl.DataFrame, *, side: str, level: int, product: str,
                normalize: bool) -> go.Scattergl | None:
    sub = df.filter((pl.col("side") == side) & (pl.col("level") == level))
    sub = sub.filter(pl.col("wall_mid").is_not_null())
    if sub.is_empty():
        return None
    style = _LEVEL_STYLE[level]
    sizes = _size_from_volume(sub["volume"].to_list(), style["size_min"], style["size_max"])
    color = "#1a9850" if side == "bid" else "#d73027"

    prices = sub["price"].to_list()
    walls = sub["wall_mid"].to_list()
    y = [p - w for p, w in zip(prices, walls)] if normalize else prices

    cd = list(zip(prices, walls, sub["volume"].to_list(),
                  [level] * sub.height,
                  sub["day"].to_list(),
                  sub["timestamp"].to_list()))
    hover = (
        f"<b>{product} {side.upper()} L{level}</b><br>"
        "price=%{customdata[0]}<br>"
        "wall_mid=%{customdata[1]}<br>"
        "y=%{y}<br>"
        "volume=%{customdata[2]}<br>"
        "level=%{customdata[3]}<br>"
        "day=%{customdata[4]}<br>"
        "ts=%{customdata[5]}<extra></extra>"
    )
    return go.Scattergl(
        x=sub["global_ts"].to_list(), y=y, mode="markers",
        marker=dict(color=color, symbol="circle", size=sizes,
                    opacity=style["opacity"], line=dict(width=0)),
        name=f"{side.upper()} L{level}",
        legendgroup=f"{side}_L{level}",
        customdata=cd, hovertemplate=hover,
    )


def _trade_traces(trades: pl.DataFrame, product: str,
                  *, normalize: bool,
                  volume_filter: Iterable[float] | None = None) -> list[go.Scattergl]:
    sub = trades.filter(pl.col("wall_mid").is_not_null())
    if volume_filter is not None:
        allowed = list(volume_filter)
        sub = sub.filter(pl.col("quantity").is_in(allowed))
    if sub.is_empty():
        return []
    sub = sub.with_columns((pl.col("price") - pl.col("wall_mid")).alias("delta"))

    out: list[go.Scattergl] = []
    specs = [
        ("Taker BUY (px>wall)",  _BUY_COLOR,  "buys",  pl.col("delta") > 0),
        ("Taker SELL (px<wall)", _SELL_COLOR, "sells", pl.col("delta") < 0),
    ]
    for name, color, lg, cond in specs:
        part = sub.filter(cond)
        if part.is_empty():
            continue
        sizes = _size_from_volume(part["quantity"].to_list(), lo=3, hi=9)
        prices = part["price"].to_list()
        walls = part["wall_mid"].to_list()
        y = part["delta"].to_list() if normalize else prices
        cd = list(zip(prices, walls,
                      part["quantity"].to_list(),
                      part["day"].to_list(),
                      part["timestamp"].to_list()))
        out.append(go.Scattergl(
            x=part["global_ts"].to_list(), y=y, mode="markers",
            marker=dict(color=color, symbol="x", size=sizes, opacity=0.9,
                        line=dict(width=1, color=color)),
            name=name, legendgroup=f"trades_{lg}",
            customdata=cd,
            hovertemplate=(
                f"<b>{product} {name}</b><br>"
                "price=%{customdata[0]}<br>"
                "wall_mid=%{customdata[1]}<br>"
                "y=%{y}<br>"
                "qty=%{customdata[2]}<br>"
                "day=%{customdata[3]}<br>"
                "ts=%{customdata[4]}<extra></extra>"
            ),
        ))
    return out


def _fair_trace(ctx: Context, product: str, *, normalize: bool,
                show_legend: bool = True) -> go.Scattergl | None:
    """wall_mid 的时间轨迹；normalize 模式下画 wall_mid - wall_mid[0]（累计变动）。"""
    wm = (
        ctx.ob_wide.filter(pl.col("product") == product)
        .select(["global_ts", "wall_mid"])
        .drop_nulls()
        .sort("global_ts")
    )
    if wm.is_empty():
        return None
    walls = wm["wall_mid"].to_list()
    base = walls[0] if normalize else 0.0
    y = [w - base for w in walls]
    name = "wall_mid drift" if normalize else "wall_mid (fair)"
    return go.Scattergl(
        x=wm["global_ts"].to_list(), y=y, mode="lines",
        line=dict(color=_FAIR_COLOR, width=1.4, dash="dash"),
        name=name, legendgroup="fair", showlegend=show_legend,
        hovertemplate=(
            f"<b>{product} fair</b><br>"
            "y=%{y}<br>ts=%{x}<extra></extra>"
        ),
    )


def _day_vlines(fig: go.Figure, days: list[int], *, row=None, col=None):
    for d in days[1:]:
        kw = dict(line=dict(color="rgba(0,0,0,0.35)", width=1, dash="dash"))
        if row is not None:
            kw["row"], kw["col"] = row, col
        fig.add_vline(x=d * 1_000_000, **kw)


def _zero_line(fig: go.Figure, *, row=None, col=None):
    kw = dict(line=dict(color="rgba(30,30,30,0.4)", width=1, dash="dot"))
    if row is not None:
        kw["row"], kw["col"] = row, col
    fig.add_hline(y=0, **kw)


# ---------------- 单产品 ----------------

def plot_product_detail(
    ctx: Context,
    product: str,
    *,
    height: int = 620,
    show_trades: bool = True,
    normalize: bool = True,
    trade_volume_filter: Iterable[float] | None = None,
) -> go.Figure:
    """单产品盘口+成交细图。

    trade_volume_filter: 若给定（成交量集合），只显示 quantity 落在该集合中的成交。

    normalize=True  : y = price − wall_mid；黑虚线 = wall_mid 累计变动。
    normalize=False : y = price (原尺度)；黑虚线 = wall_mid 本身。
    """
    ob_long = _ob_long_with_wm(ctx, product)
    fig = go.Figure()

    for side in ("bid", "ask"):
        for level in (1, 2, 3):
            tr = _book_trace(ob_long, side=side, level=level, product=product,
                             normalize=normalize)
            if tr is not None:
                fig.add_trace(tr)

    if show_trades:
        for tr in _trade_traces(_trades_with_wm(ctx, product), product,
                                normalize=normalize,
                                volume_filter=trade_volume_filter):
            fig.add_trace(tr)

    fair = _fair_trace(ctx, product, normalize=normalize)
    if fair is not None:
        fig.add_trace(fair)

    if normalize:
        _zero_line(fig)
    _day_vlines(fig, ctx.days)

    mode_tag = "normalized: price − wall_mid" if normalize else "raw price"
    fig.update_layout(
        height=height,
        title=f"{product} —— {mode_tag}; 绿=bid 红=ask 紫=主动买 黄=主动卖 黑虚=fair",
        xaxis=dict(title="global_ts", showspikes=True, spikemode="across", spikesnap="cursor"),
        yaxis=dict(title=("price − wall_mid" if normalize else "price"), zeroline=False),
        hovermode="closest",
        legend=dict(orientation="h", y=-0.15),
    )
    return fig


# ---------------- Voucher grid ----------------

def plot_depth_grid(
    ctx: Context,
    *,
    rows: int = 5, cols: int = 2,
    height: int = 1500,
    show_trades: bool = True,
    only_level_1: bool = False,
    normalize: bool = True,
    trade_volume_filter: Iterable[float] | None = None,
) -> go.Figure:
    strikes = [k for k in VOUCHER_STRIKES if f"VEV_{k}" in ctx.products]
    titles = [f"VEV_{k}" for k in strikes]

    fig = make_subplots(
        rows=rows, cols=cols, shared_xaxes=True,
        subplot_titles=titles,
        vertical_spacing=0.03, horizontal_spacing=0.06,
    )

    levels = (1,) if only_level_1 else (1, 2, 3)
    for i, k in enumerate(strikes):
        r, c = i // cols + 1, i % cols + 1
        sym = f"VEV_{k}"
        ob_long = _ob_long_with_wm(ctx, sym)

        for side in ("bid", "ask"):
            for level in levels:
                tr = _book_trace(ob_long, side=side, level=level, product=sym,
                                 normalize=normalize)
                if tr is None:
                    continue
                tr.showlegend = (i == 0)
                fig.add_trace(tr, row=r, col=c)

        if show_trades:
            for tr in _trade_traces(_trades_with_wm(ctx, sym), sym, normalize=normalize,
                                    volume_filter=trade_volume_filter):
                tr.showlegend = (i == 0)
                fig.add_trace(tr, row=r, col=c)

        fair = _fair_trace(ctx, sym, normalize=normalize, show_legend=(i == 0))
        if fair is not None:
            fig.add_trace(fair, row=r, col=c)

        if normalize:
            _zero_line(fig, row=r, col=c)
        _day_vlines(fig, ctx.days, row=r, col=c)
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", row=r, col=c)
        fig.update_yaxes(title_text=("px − wall_mid" if normalize else "price"), row=r, col=c)

    mode_tag = "y = price − wall_mid + 黑虚 wall_mid drift" if normalize \
        else "y = price + 黑虚 wall_mid (fair)"
    fig.update_layout(
        height=height, hovermode="closest",
        title=f"Voucher 盘口 + 成交 ({mode_tag}; 紫=主动买, 黄=主动卖)",
        legend=dict(orientation="h", y=1.03),
    )
    return fig


def plot_underlying_detail(ctx: Context, **kw) -> go.Figure:
    return plot_product_detail(ctx, UNDERLYING, **kw)
