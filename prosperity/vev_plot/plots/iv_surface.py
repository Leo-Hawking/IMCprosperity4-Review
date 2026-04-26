"""Implied-volatility surface overlay for all vouchers in one 3D Plotly figure.

Axes:
  x: global_ts
  y: strike
  z: implied volatility (from option mid via Black-Scholes inversion)
"""
from __future__ import annotations

import math
from statistics import NormalDist

import plotly.graph_objects as go
import polars as pl

from ..context import Context
from ..markers import strike_color

_N = NormalDist()


def _bs_call_price(spot: float, strike: float, tte_years: float, sigma: float, rate: float) -> float:
    sqrt_t = math.sqrt(tte_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tte_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * _N.cdf(d1) - strike * math.exp(-rate * tte_years) * _N.cdf(d2)


def _implied_vol_call(
    call_price: float,
    spot: float,
    strike: float,
    tte_years: float,
    *,
    rate: float,
    sigma_lo: float,
    sigma_hi: float,
    tol: float,
    max_iter: int,
    price_eps: float,
) -> float | None:
    if call_price <= 0 or spot <= 0 or strike <= 0 or tte_years <= 0:
        return None

    # No-arbitrage bounds for a call with non-dividend underlying.
    lower = max(spot - strike * math.exp(-rate * tte_years), 0.0)
    upper = spot
    if call_price <= lower + price_eps or call_price >= upper - price_eps:
        return None

    lo_p = _bs_call_price(spot, strike, tte_years, sigma_lo, rate)
    hi_p = _bs_call_price(spot, strike, tte_years, sigma_hi, rate)
    if call_price < lo_p or call_price > hi_p:
        return None

    lo, hi = sigma_lo, sigma_hi
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        mid_p = _bs_call_price(spot, strike, tte_years, mid, rate)
        err = mid_p - call_price
        if abs(err) <= tol:
            return mid
        if err > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def _iv_samples(
    ctx: Context,
    *,
    include: list[int] | None,
    sample_every: int,
    rate: float,
    sigma_lo: float,
    sigma_hi: float,
    tol: float,
    max_iter: int,
    price_eps: float,
) -> pl.DataFrame:
    strike_filter = set(include) if include else None
    panel = (
        ctx.voucher_panel()
        .select([
            "product",
            "strike",
            "day",
            "timestamp",
            "global_ts",
            "wall_mid",
            "tte_days",
            "tte_years",
            "underlying_mid",
        ])
        .filter(
            pl.col("strike").is_not_null()
            & pl.col("wall_mid").is_not_null()
            & pl.col("underlying_mid").is_not_null()
            & pl.col("tte_years").is_not_null()
            & (pl.col("tte_years") > 0)
            & (pl.col("wall_mid") > 0)
            & (pl.col("underlying_mid") > 0)
        )
    )
    if sample_every > 1:
        panel = panel.with_row_index("_idx").filter(pl.col("_idx") % sample_every == 0).drop("_idx")

    rows: list[tuple[str, int, int, int, int, float, float, float, float]] = []
    for product, strike, day, ts, gts, wall_mid, tte_d, tte_y, spot in panel.iter_rows():
        k = int(strike)
        if strike_filter is not None and k not in strike_filter:
            continue
        iv = _implied_vol_call(
            call_price=float(wall_mid),
            spot=float(spot),
            strike=float(k),
            tte_years=float(tte_y),
            rate=rate,
            sigma_lo=sigma_lo,
            sigma_hi=sigma_hi,
            tol=tol,
            max_iter=max_iter,
            price_eps=price_eps,
        )
        if iv is None:
            continue
        rows.append((product, k, int(day), int(ts), int(gts), float(wall_mid), float(tte_d), float(spot), float(iv)))

    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows,
        schema=[
            "product",
            "strike",
            "day",
            "timestamp",
            "global_ts",
            "wall_mid",
            "tte_days",
            "underlying_mid",
            "iv",
        ],
        orient="row",
    ).sort(["strike", "global_ts"])


def plot_iv_surface_overlay(
    ctx: Context,
    *,
    include: list[int] | None = None,
    sample_every: int = 40,
    rate: float = 0.0,
    sigma_lo: float = 1e-4,
    sigma_hi: float = 3.0,
    tol: float = 1e-6,
    max_iter: int = 100,
    price_eps: float = 1e-6,
    ribbon_half_width: float = 18.0,
    surface_opacity: float = 0.32,
    height: int = 760,
) -> go.Figure:
    """Plot all voucher IV ribbons in one 3D figure.

    Each strike is rendered as a thin surface ribbon around its strike level,
    then overlaid in one Plotly scene.
    """
    iv_df = _iv_samples(
        ctx,
        include=include,
        sample_every=sample_every,
        rate=rate,
        sigma_lo=sigma_lo,
        sigma_hi=sigma_hi,
        tol=tol,
        max_iter=max_iter,
        price_eps=price_eps,
    )
    if iv_df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="IV surface overlay (no valid points after filters)")
        return fig

    fig = go.Figure()
    strikes = sorted(iv_df["strike"].unique().to_list())

    for k in strikes:
        sym = f"VEV_{int(k)}"
        sub = iv_df.filter(pl.col("strike") == k)
        if sub.height < 2:
            continue

        x = sub["global_ts"].to_list()
        z = sub["iv"].to_list()
        n = len(x)
        color = strike_color(int(k))

        fig.add_trace(
            go.Surface(
                x=[x, x],
                y=[[k - ribbon_half_width] * n, [k + ribbon_half_width] * n],
                z=[z, z],
                surfacecolor=[[0.0] * n, [1.0] * n],
                colorscale=[[0.0, color], [1.0, color]],
                cmin=0.0,
                cmax=1.0,
                showscale=False,
                opacity=surface_opacity,
                name=sym,
                legendgroup=sym,
                showlegend=True,
                hoverinfo="skip",
            )
        )

        custom = list(
            zip(
                sub["day"].to_list(),
                sub["timestamp"].to_list(),
                sub["tte_days"].to_list(),
                sub["underlying_mid"].to_list(),
                sub["wall_mid"].to_list(),
            )
        )
        fig.add_trace(
            go.Scatter3d(
                x=x,
                y=[int(k)] * n,
                z=z,
                mode="lines",
                line=dict(color=color, width=4),
                name=f"{sym} center",
                legendgroup=sym,
                showlegend=False,
                customdata=custom,
                hovertemplate=(
                    f"<b>{sym}</b><br>"
                    "iv=%{z:.2%}<br>"
                    "strike=%{y}<br>"
                    "day=%{customdata[0]}<br>"
                    "ts=%{customdata[1]}<br>"
                    "tte_days=%{customdata[2]:.2f}<br>"
                    "S_mid=%{customdata[3]:.2f}<br>"
                    "C_wall_mid=%{customdata[4]:.2f}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        height=height,
        title="IV surface overlay by voucher (x=time, y=strike, z=implied vol)",
        legend=dict(orientation="h", y=1.02, x=0.0),
        margin=dict(l=0, r=0, b=0, t=55),
        scene=dict(
            xaxis=dict(title="global_ts (= day * 1M + timestamp)"),
            yaxis=dict(title="strike"),
            zaxis=dict(title="implied vol", tickformat=".2%"),
            camera=dict(eye=dict(x=1.55, y=1.25, z=0.95)),
        ),
    )
    return fig
