"""盘口深度 + 成交位置散点图。

支持两种模式：
    - normalize=True  : y = price - fair_price，fair price 叠成浅色虚线（右轴）。
    - normalize=False : y = price (原尺度)，fair price 叠成浅色实线。

其他约定：
  - Bid = 绿, Ask = 红；bubble 比初版小一圈。
  - Trades 每笔同时画 buyer 与 seller 两个 marker，不丢失任意一方：
      颜色 = bot 身份（同一个 bot 在所有图里固定颜色）
      形状 = 该 bot 在该笔的方向：buyer→triangle-up；seller→triangle-down
    wall_mid 仅用于 near-wall 判断与方向推断，normalize 使用 fair_price。
"""
from __future__ import annotations

import re
from typing import Iterable

import plotly.colors as _pc
import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from ..context import Context
from ..dataio import UNDERLYING, VOUCHER_STRIKES


# Bot color palette — color encodes bot identity; direction (buy/sell) is encoded
# by the marker shape (triangle-up / triangle-down), see _trade_traces.
# 选取高饱和、跨色相分布的 24 色，相邻 index 颜色尽量错开。
_BOT_COLORS = [
    "#e6194b",  # red
    "#3cb44b",  # green
    "#4363d8",  # blue
    "#f58231",  # orange
    "#911eb4",  # purple
    "#42d4f4",  # cyan
    "#f032e6",  # magenta
    "#9a6324",  # brown
    "#469990",  # teal
    "#bfef45",  # lime
    "#800000",  # maroon
    "#000075",  # navy
    "#808000",  # olive
    "#e6beff",  # lavender
    "#fabed4",  # pink
    "#aaffc3",  # mint
    "#ffd8b1",  # apricot
    "#dcbeff",  # mauve
    "#a9a9a9",  # gray
    "#ff4500",  # orangered
    "#1e90ff",  # dodgerblue
    "#daa520",  # goldenrod
    "#2e8b57",  # seagreen
    "#c71585",  # mediumvioletred
]


def _bot_color(name: str) -> str:
    """Deterministic color for a bot name. Uses the trailing integer when
    present (e.g. 'Mark 14') so the same bot always gets the same color
    regardless of which subset of trades is plotted."""
    m = re.search(r"\d+", name or "")
    idx = int(m.group()) if m else sum(map(ord, name or "?"))
    return _BOT_COLORS[idx % len(_BOT_COLORS)]


_LEVEL_STYLE = {
    1: dict(size_min=1.5, size_max=6.0, opacity=0.80),
    2: dict(size_min=1.0, size_max=4.5, opacity=0.45),
    3: dict(size_min=1.0, size_max=3.5, opacity=0.28),
}

_BUY_COLOR  = "#8e44ad"   # 紫：主动买
_SELL_COLOR = "#f1c40f"   # 黄：主动卖
_FAIR_COLOR = "rgba(30,30,30,0.75)"
_FAIR_COLOR_LIGHT = "rgba(120,120,180,0.55)"   # 浅蓝灰：normalized 模式下 fair_price 的浅色

# Single style for merged bid/ask traces (no L1/L2/L3 distinction).
_BOOK_STYLE = dict(size_min=1.5, size_max=7.0, opacity=0.55)


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
    fair = (
        ctx.fair_df.filter(pl.col("product") == product)
        .select(["day", "timestamp", "fair"])
    )
    return sub.join(wm, on=["day", "timestamp"], how="left").join(
        fair, on=["day", "timestamp"], how="left"
    )


def _trades_with_wm(ctx: Context, product: str) -> pl.DataFrame:
    tr = ctx.trades.filter(pl.col("product") == product).sort(["day", "timestamp"])
    if tr.is_empty():
        return tr
    wm = (
        ctx.ob_wide.filter(pl.col("product") == product)
        .with_columns(
            ((pl.col("bid_price_1") + pl.col("ask_price_1")) / 2).alias("mid_1")
        )
        .select(["day", "timestamp", "wall_mid", "mid_1"])
        .sort(["day", "timestamp"])
    )
    fair = (
        ctx.fair_df.filter(pl.col("product") == product)
        .select(["day", "timestamp", "fair"])
        .sort(["day", "timestamp"])
    )
    return tr.join_asof(wm, on="timestamp", by="day", strategy="backward").join_asof(
        fair, on="timestamp", by="day", strategy="backward"
    )


# ---------------- Traces ----------------

def _book_trace(df: pl.DataFrame, *, side: str, product: str,
                normalize: bool) -> go.Scattergl | None:
    """合并 L1/L2/L3 为一个 trace —— 同一边一种颜色一种 marker。"""
    sub = df.filter(pl.col("side") == side)
    sub = sub.filter(pl.col("wall_mid").is_not_null())
    if sub.is_empty():
        return None
    sizes = _size_from_volume(
        sub["volume"].to_list(), _BOOK_STYLE["size_min"], _BOOK_STYLE["size_max"]
    )
    color = "#1a9850" if side == "bid" else "#d73027"

    prices = sub["price"].to_list()
    walls = sub["wall_mid"].to_list()
    fairs = sub["fair"].to_list() if "fair" in sub.columns else walls
    y = [p - f for p, f in zip(prices, fairs)] if normalize else prices

    cd = list(zip(prices, walls, fairs, sub["volume"].to_list(),
                  sub["level"].to_list(),
                  sub["day"].to_list(),
                  sub["timestamp"].to_list()))
    hover = (
        f"<b>{product} {side.upper()}</b><br>"
        "price=%{customdata[0]}<br>"
        "wall_mid=%{customdata[1]}<br>"
        "fair=%{customdata[2]}<br>"
        "y=%{y}<br>"
        "volume=%{customdata[3]}<br>"
        "level=%{customdata[4]}<br>"
        "day=%{customdata[5]}<br>"
        "ts=%{customdata[6]}<extra></extra>"
    )
    return go.Scattergl(
        x=sub["global_ts"].to_list(), y=y, mode="markers",
        marker=dict(color=color, symbol="circle", size=sizes,
                    opacity=_BOOK_STYLE["opacity"], line=dict(width=0)),
        name=f"{side.upper()}",
        legendgroup=f"{side}",
        customdata=cd, hovertemplate=hover,
    )


def _book_near_trace(df: pl.DataFrame, *, side: str, product: str,
                     normalize: bool) -> go.Scattergl | None:
    """单独的 trace：只画 |price − wall_mid| ≤ 1 的近墙订单。"""
    sub = df.filter(pl.col("side") == side)
    sub = sub.filter(pl.col("wall_mid").is_not_null())
    if sub.is_empty():
        return None
    prices = sub["price"].to_list()
    walls = sub["wall_mid"].to_list()
    fairs = sub["fair"].to_list() if "fair" in sub.columns else walls
    vols = sub["volume"].to_list()
    levels = sub["level"].to_list()
    days = sub["day"].to_list()
    ts = sub["timestamp"].to_list()
    gts = sub["global_ts"].to_list()

    keep = [i for i, (p, w) in enumerate(zip(prices, walls)) if abs(p - w) <= 1]
    if not keep:
        return None

    kx = [gts[i] for i in keep]
    kp = [prices[i] for i in keep]
    kw = [walls[i] for i in keep]
    kf = [fairs[i] for i in keep]
    kv = [vols[i] for i in keep]
    ky = [p - f for p, f in zip(kp, kf)] if normalize else kp
    sizes = _size_from_volume(kv, 4.0, 10.0)
    color = "#0b6623" if side == "bid" else "#7f1d1d"  # 深绿 / 深红，区别于普通 bid/ask

    cd = list(zip(kp, kw, kf, kv,
                  [levels[i] for i in keep],
                  [days[i] for i in keep],
                  [ts[i] for i in keep]))
    hover = (
        f"<b>{product} {side.upper()} NEAR</b><br>"
        "price=%{customdata[0]}<br>"
        "wall_mid=%{customdata[1]}<br>"
        "fair=%{customdata[2]}<br>"
        "y=%{y}<br>"
        "volume=%{customdata[3]}<br>"
        "level=%{customdata[4]}<br>"
        "day=%{customdata[5]}<br>"
        "ts=%{customdata[6]}<extra></extra>"
    )
    return go.Scattergl(
        x=kx, y=ky, mode="markers",
        marker=dict(color=color, symbol="diamond-open", size=sizes,
                    opacity=0.95, line=dict(width=1.4, color=color)),
        name=f"{side.upper()} near wall",
        legendgroup=f"{side}_near",
        customdata=cd, hovertemplate=hover,
    )


def _trade_traces(trades: pl.DataFrame, product: str,
                  *, normalize: bool,
                  volume_filter: Iterable[float] | None = None) -> list[go.Scattergl]:
    """每笔成交同时画 buyer 与 seller 两个 marker：
      - 颜色 = bot 身份（_bot_color）
      - 形状 = 该 bot 在此笔的方向：buyer→triangle-up，seller→triangle-down
    这样不会因为只画 taker 而丢失另一方的信息。
    每个 (bot, 方向) 一个 trace，便于图例独立 toggle。
    """
    sub = trades.filter(pl.col("wall_mid").is_not_null())
    if volume_filter is not None:
        allowed = list(volume_filter)
        sub = sub.filter(pl.col("quantity").is_in(allowed))
    if sub.is_empty():
        return []
    sub = sub.with_columns([
        (pl.col("price") - pl.col("wall_mid")).alias("delta_wm"),
        (pl.col("price") - pl.col("fair")).alias("delta_fair"),
    ])

    has_buyer = "buyer" in sub.columns
    has_seller = "seller" in sub.columns
    if not (has_buyer or has_seller):
        return []

    # Long-form: 每笔成交 → 至多两条记录（buyer / seller 各一条）。
    # 当 buyer/seller 都为空（如 round 5），按 price vs wall_mid 推断 taker 方向：
    #   price > wall_mid → taker 主动买 (Unknown buyer)
    #   price < wall_mid → taker 主动卖 (Unknown seller)
    parts: list[pl.DataFrame] = []
    base_cols = ["global_ts", "day", "timestamp", "price", "wall_mid", "fair",
                 "quantity", "delta_wm", "delta_fair"]
    if has_buyer:
        parts.append(
            sub.filter(pl.col("buyer").fill_null("") != "")
            .select(base_cols + [pl.col("buyer").alias("bot"),
                                 pl.lit("buy").alias("side")])
        )
    if has_seller:
        parts.append(
            sub.filter(pl.col("seller").fill_null("") != "")
            .select(base_cols + [pl.col("seller").alias("bot"),
                                 pl.lit("sell").alias("side")])
        )

    # Fallback for trades where both buyer and seller are blank.
    # 按 price vs wall_mid 推断 taker 方向；price == wall_mid 用中性标记。
    buyer_empty = pl.col("buyer").fill_null("") == "" if has_buyer else pl.lit(True)
    seller_empty = pl.col("seller").fill_null("") == "" if has_seller else pl.lit(True)
    anon = sub.filter(buyer_empty & seller_empty)
    if not anon.is_empty():
        parts.append(
            anon.filter(pl.col("delta_wm") > 0)
            .select(base_cols + [pl.lit("Unknown").alias("bot"),
                                 pl.lit("buy").alias("side")])
        )
        parts.append(
            anon.filter(pl.col("delta_wm") < 0)
            .select(base_cols + [pl.lit("Unknown").alias("bot"),
                                 pl.lit("sell").alias("side")])
        )
        parts.append(
            anon.filter(pl.col("delta_wm") == 0)
            .select(base_cols + [pl.lit("Unknown").alias("bot"),
                                 pl.lit("flat").alias("side")])
        )

    if not parts:
        return []
    long = pl.concat([p for p in parts if not p.is_empty()], how="vertical") \
        if any(not p.is_empty() for p in parts) else pl.DataFrame()
    if long.is_empty():
        return []

    out: list[go.Scattergl] = []
    bots = sorted(long["bot"].unique().to_list())
    side_specs = (
        ("buy",  "triangle-up"),
        ("sell", "triangle-down"),
        ("flat", "circle"),         # price == wall_mid, only used for Unknown
    )
    _FLAT_COLOR = "rgba(120,120,120,0.85)"
    for bot in bots:
        for side, symbol in side_specs:
            # Anonymous trades use the doc'd palette: 紫=主动买，黄=主动卖，灰=持平。
            if bot == "Unknown":
                color = (_BUY_COLOR if side == "buy"
                         else _SELL_COLOR if side == "sell"
                         else _FLAT_COLOR)
            else:
                if side == "flat":
                    continue  # named bots always have explicit buy/sell
                color = _bot_color(bot)
            part = long.filter((pl.col("bot") == bot) & (pl.col("side") == side))
            if part.is_empty():
                continue
            sizes = _size_from_volume(part["quantity"].to_list(), lo=4, hi=10)
            prices = part["price"].to_list()
            walls = part["wall_mid"].to_list()
            fairs = part["fair"].to_list()
            y = part["delta_fair"].to_list() if normalize else prices
            cd = list(zip(prices, walls, fairs,
                          part["quantity"].to_list(),
                          part["day"].to_list(),
                          part["timestamp"].to_list(),
                          part["bot"].to_list(),
                          part["delta_wm"].to_list(),
                          part["delta_fair"].to_list(),
                          part["side"].to_list()))
            out.append(go.Scattergl(
                x=part["global_ts"].to_list(), y=y, mode="markers",
                marker=dict(color=color, symbol=symbol, size=sizes, opacity=0.9,
                            line=dict(width=1, color=color)),
                name=f"{bot} {side}", legendgroup=f"bot_{bot}_{side}",
                customdata=cd,
                hovertemplate=(
                    f"<b>{product} %{{customdata[6]}} %{{customdata[9]}}</b><br>"
                    "price=%{customdata[0]}<br>"
                    "wall_mid=%{customdata[1]}<br>"
                    "fair=%{customdata[2]}<br>"
                    "delta_wm=%{customdata[7]}<br>"
                    "delta_fair=%{customdata[8]}<br>"
                    "y=%{y}<br>"
                    "qty=%{customdata[3]}<br>"
                    "day=%{customdata[4]}<br>"
                    "ts=%{customdata[5]}<extra></extra>"
                ),
            ))
    return out


def _trade_near_trace(trades: pl.DataFrame, product: str,
                      *, normalize: bool,
                      volume_filter: Iterable[float] | None = None) -> go.Scattergl | None:
    """单独的 trace：只画 |price − wall_mid| ≤ 1 的近墙成交。"""
    sub = trades.filter(pl.col("wall_mid").is_not_null())
    if volume_filter is not None:
        sub = sub.filter(pl.col("quantity").is_in(list(volume_filter)))
    if sub.is_empty():
        return None
    sub = sub.with_columns([
        (pl.col("price") - pl.col("wall_mid")).alias("delta_wm"),
        (pl.col("price") - pl.col("fair")).alias("delta_fair"),
    ])
    sub = sub.filter(pl.col("delta_wm").abs() <= 1)
    if sub.is_empty():
        return None
    prices = sub["price"].to_list()
    walls = sub["wall_mid"].to_list()
    fairs = sub["fair"].to_list()
    deltas = sub["delta_fair"].to_list()
    y = deltas if normalize else prices
    sizes = _size_from_volume(sub["quantity"].to_list(), lo=5, hi=12)
    cd = list(zip(prices, walls, fairs,
                  sub["quantity"].to_list(),
                  sub["day"].to_list(),
                  sub["timestamp"].to_list(),
                  deltas))
    return go.Scattergl(
        x=sub["global_ts"].to_list(), y=y, mode="markers",
        marker=dict(color="rgba(0,0,0,0)", symbol="diamond-open", size=sizes,
                    opacity=1.0, line=dict(width=1.6, color="black")),
        name="trade near wall", legendgroup="trade_near",
        customdata=cd,
        hovertemplate=(
            f"<b>{product} TRADE NEAR</b><br>"
            "price=%{customdata[0]}<br>"
            "wall_mid=%{customdata[1]}<br>"
            "fair=%{customdata[2]}<br>"
            "delta_fair=%{customdata[6]}<br>"
            "y=%{y}<br>"
            "qty=%{customdata[3]}<br>"
            "day=%{customdata[4]}<br>"
            "ts=%{customdata[5]}<extra></extra>"
        ),
    )


def _fair_trace(ctx: Context, product: str, *, normalize: bool,
                show_legend: bool = True,
                yaxis: str | None = None) -> go.Scattergl | None:
    """fair_price 时间轨迹；normalize 模式下放到独立 y 轴上（浅色）。"""
    fair = (
        ctx.fair_df.filter(pl.col("product") == product)
        .select(["global_ts", "fair"])
        .drop_nulls()
        .sort("global_ts")
    )
    if fair.is_empty():
        return None
    vals = fair["fair"].to_list()
    if normalize:
        # 单独 y 轴：直接画 fair 原值，便于和 normalized 主轴脱耦
        y = vals
        name = "fair_price"
        color = _FAIR_COLOR_LIGHT
        dash = "dash"
    else:
        y = vals
        name = "fair_price"
        color = _FAIR_COLOR_LIGHT
        dash = "solid"
    kw: dict = dict(
        x=fair["global_ts"].to_list(), y=y, mode="lines",
        line=dict(color=color, width=1.4, dash=dash),
        name=name, legendgroup="fair", showlegend=show_legend,
        hovertemplate=(
            f"<b>{product} fair</b><br>"
            "fair=%{y}<br>ts=%{x}<extra></extra>"
        ),
    )
    if yaxis is not None:
        kw["yaxis"] = yaxis
    return go.Scattergl(**kw)


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

    normalize=True  : y = price − fair_price；浅色虚线 = fair_price。
    normalize=False : y = price (原尺度)；浅色实线 = fair_price。
    """
    ob_long = _ob_long_with_wm(ctx, product)
    fig = go.Figure()

    for side in ("bid", "ask"):
        tr = _book_trace(ob_long, side=side, product=product, normalize=normalize)
        if tr is not None:
            fig.add_trace(tr)
    for side in ("bid", "ask"):
        tr = _book_near_trace(ob_long, side=side, product=product, normalize=normalize)
        if tr is not None:
            fig.add_trace(tr)

    if show_trades:
        trades_wm = _trades_with_wm(ctx, product)
        for tr in _trade_traces(trades_wm, product,
                                normalize=normalize,
                                volume_filter=trade_volume_filter):
            fig.add_trace(tr)
        nt = _trade_near_trace(trades_wm, product, normalize=normalize,
                               volume_filter=trade_volume_filter)
        if nt is not None:
            fig.add_trace(nt)

    fair_yaxis = "y2" if normalize else None
    fair = _fair_trace(ctx, product, normalize=normalize, yaxis=fair_yaxis)
    if fair is not None:
        fig.add_trace(fair)

    if normalize:
        _zero_line(fig)
    _day_vlines(fig, ctx.days)

    mode_tag = "normalized: price − fair_price" if normalize else "raw price"
    layout: dict = dict(
        height=height,
        title=f"{product} —— {mode_tag}; 绿=bid 红=ask 颜色=bot ▲=buy ▼=sell 浅虚=fair_price",
        xaxis=dict(title="global_ts", showspikes=True, spikemode="across", spikesnap="cursor"),
        yaxis=dict(title=("price − fair_price" if normalize else "price"), zeroline=False),
        hovermode="closest",
        legend=dict(orientation="h", y=-0.15),
    )
    if normalize:
        layout["yaxis2"] = dict(
            title="fair_price",
            overlaying="y",
            side="right",
            showgrid=False,
            color=_FAIR_COLOR_LIGHT,
        )
    fig.update_layout(**layout)
    return fig


# ---------------- Voucher grid ----------------

def plot_depth_grid(
    ctx: Context,
    *,
    rows: int = 10, cols: int = 1,
    height: int = 2600,
    show_trades: bool = True,
    only_level_1: bool = False,   # kept for back-compat; ignored (levels now merged)
    normalize: bool = True,
    trade_volume_filter: Iterable[float] | None = None,
) -> go.Figure:
    strikes = [k for k in VOUCHER_STRIKES if f"VEV_{k}" in ctx.products]
    titles = [f"VEV_{k}" for k in strikes]

    # 在 normalize 模式下为每个子图配一条独立的右侧 y 轴（用于 fair_price 浅色覆盖）。
    specs = [[{"secondary_y": normalize}] * cols for _ in range(rows)]

    fig = make_subplots(
        rows=rows, cols=cols, shared_xaxes=True,
        subplot_titles=titles,
        vertical_spacing=0.025, horizontal_spacing=0.06,
        specs=specs,
    )

    for i, k in enumerate(strikes):
        r, c = i // cols + 1, i % cols + 1
        sym = f"VEV_{k}"
        ob_long = _ob_long_with_wm(ctx, sym)

        for side in ("bid", "ask"):
            tr = _book_trace(ob_long, side=side, product=sym, normalize=normalize)
            if tr is None:
                continue
            tr.showlegend = (i == 0)
            fig.add_trace(tr, row=r, col=c, secondary_y=False)
        for side in ("bid", "ask"):
            tr = _book_near_trace(ob_long, side=side, product=sym, normalize=normalize)
            if tr is None:
                continue
            tr.showlegend = (i == 0)
            fig.add_trace(tr, row=r, col=c, secondary_y=False)

        if show_trades:
            trades_wm = _trades_with_wm(ctx, sym)
            for tr in _trade_traces(trades_wm, sym, normalize=normalize,
                                    volume_filter=trade_volume_filter):
                tr.showlegend = (i == 0)
                fig.add_trace(tr, row=r, col=c, secondary_y=False)
            nt = _trade_near_trace(trades_wm, sym, normalize=normalize,
                                   volume_filter=trade_volume_filter)
            if nt is not None:
                nt.showlegend = (i == 0)
                fig.add_trace(nt, row=r, col=c, secondary_y=False)

        fair = _fair_trace(ctx, sym, normalize=normalize, show_legend=(i == 0))
        if fair is not None:
            fig.add_trace(fair, row=r, col=c, secondary_y=normalize)

        if normalize:
            _zero_line(fig, row=r, col=c)
        _day_vlines(fig, ctx.days, row=r, col=c)
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", row=r, col=c)
        fig.update_yaxes(
            title_text=("px − fair_price" if normalize else "price"),
            row=r, col=c, secondary_y=False,
        )
        if normalize:
            fig.update_yaxes(
                title_text="fair_price", row=r, col=c, secondary_y=True,
                showgrid=False, color=_FAIR_COLOR_LIGHT,
            )

    mode_tag = "y = price − fair_price (右轴=fair_price 浅色)" if normalize \
        else "y = price + 浅虚 fair_price"
    fig.update_layout(
        height=height, hovermode="closest",
        title=f"Voucher 盘口 + 成交 ({mode_tag}; 主动 taker = 颜色+形状 区分)",
        legend=dict(orientation="h", y=1.02),
    )
    return fig


def plot_underlying_detail(ctx: Context, **kw) -> go.Figure:
    return plot_product_detail(ctx, UNDERLYING, **kw)
