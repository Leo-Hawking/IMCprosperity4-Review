"""IV diagnostics plots aligned with Round 3 methodology.

Provided plots:
  - plot_iv_smile: IV vs moneyness with global quadratic fit (Figure 6a style)
  - plot_iv_residual: delta_iv = iv - iv_hat over time by strike (Figure 6b style)
  - plot_price_residual: delta_px = wall_mid - BS(S,K,T,iv_hat) (Figure 6c style)
  - plot_underlying_autocorr: underlying return autocorr vs Monte Carlo baseline (Figure 8 style)
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import plotly.graph_objects as go
import polars as pl

from ..context import Context
from ..dataio import UNDERLYING
from ..markers import strike_color
from .iv_surface import _iv_samples

_N = NormalDist()


def _bs_call_price(spot: np.ndarray, strike: np.ndarray, tte_years: np.ndarray, sigma: np.ndarray, rate: float) -> np.ndarray:
    sqrt_t = np.sqrt(tte_years)
    d1 = (np.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tte_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    cdf_d1 = np.vectorize(_N.cdf)(d1)
    cdf_d2 = np.vectorize(_N.cdf)(d2)
    return spot * cdf_d1 - strike * np.exp(-rate * tte_years) * cdf_d2


def _build_iv_diag_df(
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
    robust_refit: bool,
    outlier_sigma: float,
) -> tuple[pl.DataFrame, tuple[float, float, float]]:
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
    if iv_df.is_empty() or iv_df.height < 10:
        return pl.DataFrame(), (0.0, 0.0, 0.0)

    # m_t = ln(K/S) / sqrt(T)
    iv_df = iv_df.with_columns(
        (
            (pl.col("strike").cast(pl.Float64) / pl.col("underlying_mid")).log()
            / (pl.col("tte_days") / 365.0).sqrt()
        ).alias("m_t")
    ).filter(pl.col("m_t").is_finite())

    if iv_df.is_empty() or iv_df.height < 10:
        return pl.DataFrame(), (0.0, 0.0, 0.0)

    x = np.array(iv_df["m_t"].to_list(), dtype=float)
    y = np.array(iv_df["iv"].to_list(), dtype=float)

    a, b, c = np.polyfit(x, y, deg=2)

    if robust_refit and len(x) >= 30:
        y_hat_1 = a * x * x + b * x + c
        resid = y - y_hat_1
        std = float(np.std(resid))
        if std > 0:
            keep = np.abs(resid) <= outlier_sigma * std
            if int(np.sum(keep)) >= 20:
                x2, y2 = x[keep], y[keep]
                a, b, c = np.polyfit(x2, y2, deg=2)

    iv_hat = a * x * x + b * x + c
    strike = np.array(iv_df["strike"].to_list(), dtype=float)
    spot = np.array(iv_df["underlying_mid"].to_list(), dtype=float)
    tte = np.array(iv_df["tte_days"].to_list(), dtype=float) / 365.0
    theo = _bs_call_price(spot=spot, strike=strike, tte_years=tte, sigma=iv_hat, rate=rate)
    wall_mid = np.array(iv_df["wall_mid"].to_list(), dtype=float)

    iv_df = iv_df.with_columns([
        pl.Series("iv_hat", iv_hat.tolist()),
        pl.Series("delta_iv", (y - iv_hat).tolist()),
        pl.Series("bs_fair", theo.tolist()),
        pl.Series("delta_px", (wall_mid - theo).tolist()),
    ])

    return iv_df.sort(["strike", "global_ts"]), (float(a), float(b), float(c))


def plot_iv_smile(
    ctx: Context,
    *,
    include: list[int] | None = None,
    sample_every: int = 10,
    rate: float = 0.0,
    sigma_lo: float = 1e-4,
    sigma_hi: float = 3.0,
    tol: float = 1e-6,
    max_iter: int = 100,
    price_eps: float = 1e-6,
    robust_refit: bool = True,
    outlier_sigma: float = 3.0,
    marker_opacity: float = 0.45,
    marker_size: int = 5,
    height: int = 620,
) -> go.Figure:
    """Figure 6a style: IV scatter in moneyness space + global quadratic fit."""
    df, coeffs = _build_iv_diag_df(
        ctx,
        include=include,
        sample_every=sample_every,
        rate=rate,
        sigma_lo=sigma_lo,
        sigma_hi=sigma_hi,
        tol=tol,
        max_iter=max_iter,
        price_eps=price_eps,
        robust_refit=robust_refit,
        outlier_sigma=outlier_sigma,
    )
    if df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="IV smile (no valid points after filters)")
        return fig

    a, b, c = coeffs
    fig = go.Figure()
    strikes = sorted(df["strike"].unique().to_list())

    for k in strikes:
        sub = df.filter(pl.col("strike") == k)
        fig.add_trace(
            go.Scatter(
                x=sub["m_t"].to_list(),
                y=sub["iv"].to_list(),
                mode="markers",
                marker=dict(color=strike_color(int(k)), size=marker_size, opacity=marker_opacity),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                customdata=list(zip(sub["day"].to_list(), sub["timestamp"].to_list())),
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    "m_t=%{x:.4f}<br>iv=%{y:.2%}<br>"
                    "day=%{customdata[0]}<br>ts=%{customdata[1]}<extra></extra>"
                ),
            )
        )

    x = np.array(df["m_t"].to_list(), dtype=float)
    x_grid = np.linspace(np.min(x), np.max(x), 400)
    y_grid = a * x_grid * x_grid + b * x_grid + c
    fig.add_trace(
        go.Scatter(
            x=x_grid.tolist(),
            y=y_grid.tolist(),
            mode="lines",
            line=dict(color="black", width=3),
            name="Fitted parabola",
            hovertemplate="m_t=%{x:.4f}<br>iv_hat=%{y:.2%}<extra></extra>",
        )
    )

    fig.update_layout(
        height=height,
        title="IV Smile: iv(m_t) with global quadratic fit",
        xaxis=dict(title="m_t = ln(K / S_t) / sqrt(TTE_t)"),
        yaxis=dict(title="implied vol", tickformat=".2%"),
        hovermode="closest",
    )
    return fig


def plot_iv_residual(
    ctx: Context,
    *,
    include: list[int] | None = None,
    sample_every: int = 10,
    rate: float = 0.0,
    sigma_lo: float = 1e-4,
    sigma_hi: float = 3.0,
    tol: float = 1e-6,
    max_iter: int = 100,
    price_eps: float = 1e-6,
    robust_refit: bool = True,
    outlier_sigma: float = 3.0,
    height: int = 620,
) -> go.Figure:
    """Figure 6b style: delta_iv = iv - iv_hat over time by strike."""
    df, _ = _build_iv_diag_df(
        ctx,
        include=include,
        sample_every=sample_every,
        rate=rate,
        sigma_lo=sigma_lo,
        sigma_hi=sigma_hi,
        tol=tol,
        max_iter=max_iter,
        price_eps=price_eps,
        robust_refit=robust_refit,
        outlier_sigma=outlier_sigma,
    )
    if df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="IV residual (no valid points after filters)")
        return fig

    fig = go.Figure()
    for k in sorted(df["strike"].unique().to_list()):
        sub = df.filter(pl.col("strike") == k)
        fig.add_trace(
            go.Scatter(
                x=sub["global_ts"].to_list(),
                y=sub["delta_iv"].to_list(),
                mode="lines",
                line=dict(color=strike_color(int(k)), width=1.2),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    "delta_iv=%{y:.2%}<br>global_ts=%{x}<extra></extra>"
                ),
            )
        )

    fig.add_hline(y=0.0, line=dict(color="rgba(0,0,0,0.35)", width=1, dash="dash"))
    fig.update_layout(
        height=height,
        title="IV residual over time: delta_iv = iv - iv_hat(m_t)",
        xaxis=dict(title="global_ts"),
        yaxis=dict(title="delta_iv", tickformat=".2%"),
        hovermode="x unified",
    )
    return fig


def plot_price_residual(
    ctx: Context,
    *,
    include: list[int] | None = None,
    sample_every: int = 10,
    rate: float = 0.0,
    sigma_lo: float = 1e-4,
    sigma_hi: float = 3.0,
    tol: float = 1e-6,
    max_iter: int = 100,
    price_eps: float = 1e-6,
    robust_refit: bool = True,
    outlier_sigma: float = 3.0,
    height: int = 620,
) -> go.Figure:
    """Figure 6c style: delta_px = wall_mid - BS(S,K,T,iv_hat)."""
    df, _ = _build_iv_diag_df(
        ctx,
        include=include,
        sample_every=sample_every,
        rate=rate,
        sigma_lo=sigma_lo,
        sigma_hi=sigma_hi,
        tol=tol,
        max_iter=max_iter,
        price_eps=price_eps,
        robust_refit=robust_refit,
        outlier_sigma=outlier_sigma,
    )
    if df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="Price residual (no valid points after filters)")
        return fig

    fig = go.Figure()
    for k in sorted(df["strike"].unique().to_list()):
        sub = df.filter(pl.col("strike") == k)
        fig.add_trace(
            go.Scatter(
                x=sub["global_ts"].to_list(),
                y=sub["delta_px"].to_list(),
                mode="lines",
                line=dict(color=strike_color(int(k)), width=1.2),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    "delta_px=%{y:.3f}<br>global_ts=%{x}<extra></extra>"
                ),
            )
        )

    fig.add_hline(y=0.0, line=dict(color="rgba(0,0,0,0.35)", width=1, dash="dash"))
    fig.update_layout(
        height=height,
        title="Price residual over time: wall_mid - BS_theo(iv_hat)",
        xaxis=dict(title="global_ts"),
        yaxis=dict(title="delta_px (SeaShells)"),
        hovermode="x unified",
    )
    return fig


def _autocorr_at_lag(x: np.ndarray, lag: int) -> float:
    if lag <= 0 or lag >= len(x):
        return float("nan")
    x0 = x[:-lag]
    x1 = x[lag:]
    s0 = float(np.std(x0))
    s1 = float(np.std(x1))
    if s0 == 0 or s1 == 0:
        return float("nan")
    return float(np.corrcoef(x0, x1)[0, 1])


def plot_underlying_autocorr(
    ctx: Context,
    *,
    max_lag: int = 100,
    n_sims: int = 1000,
    seed: int = 0,
    height: int = 560,
) -> go.Figure:
    """Figure 8 style: underlying autocorrelation vs Monte Carlo white-noise baseline."""
    und = ctx.product_slice(UNDERLYING)
    if und.is_empty() or und.height < max_lag + 20:
        fig = go.Figure()
        fig.update_layout(title="Underlying autocorrelation (insufficient data)")
        return fig

    p = np.array(und["mid_price"].to_list(), dtype=float)
    ret = np.diff(np.log(np.maximum(p, 1e-9)))
    if ret.size < max_lag + 20:
        fig = go.Figure()
        fig.update_layout(title="Underlying autocorrelation (insufficient return length)")
        return fig

    lags = np.arange(1, max_lag + 1)
    actual = np.array([_autocorr_at_lag(ret, int(k)) for k in lags], dtype=float)

    rng = np.random.default_rng(seed)
    sigma = float(np.std(ret))
    baseline = np.empty((n_sims, max_lag), dtype=float)
    for i in range(n_sims):
        noise = rng.normal(0.0, sigma, size=ret.size)
        baseline[i, :] = np.array([_autocorr_at_lag(noise, int(k)) for k in lags], dtype=float)

    fig = go.Figure()
    for i in range(n_sims):
        fig.add_trace(
            go.Scatter(
                x=lags.tolist(),
                y=baseline[i, :].tolist(),
                mode="lines",
                line=dict(color="rgba(0,0,0,0.12)", width=0.6),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    fig.add_trace(
        go.Scatter(
            x=lags.tolist(),
            y=actual.tolist(),
            mode="lines",
            line=dict(color="red", width=2.2),
            name=UNDERLYING,
            hovertemplate="lag=%{x}<br>autocorr=%{y:.4f}<extra></extra>",
        )
    )

    fig.update_layout(
        height=height,
        title="Underlying autocorrelation vs Monte Carlo baseline",
        xaxis=dict(title="lag"),
        yaxis=dict(title="autocorrelation"),
        hovermode="closest",
    )
    return fig
