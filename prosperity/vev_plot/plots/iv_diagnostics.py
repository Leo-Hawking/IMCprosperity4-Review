"""IV diagnostics plots aligned with Round 3 methodology.

Provided plots:
  - plot_iv_smile: IV vs moneyness with global quadratic fit (Figure 6a style)
    - plot_iv_smile_3d: 3D IV smile view (x moneyness/log-moneyness, y time/TTE, z IV)
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

# Strikes used to fit the volatility surface (quadratic in moneyness).
# Other strikes still get iv_hat / residuals evaluated against this fit, but
# they do not influence the fitted parameters.
FIT_STRIKES: tuple[int, ...] = (5000,5100, 5200, 5300, 5500)

# Internal EMA half-lives (raw global_ts ticks); not exposed as plot args.
_SIGNAL_HALFLIFE: int = 2_000    # short EMA on residuals for signal_*
_SELF_EMA_HALFLIFE: int = 20_000 # slow EMA subtracted in residual plots


def _dt_ema(values: np.ndarray, ts: np.ndarray, halflife: float) -> np.ndarray:
    """dt-aware EMA. ts and halflife share units (raw global_ts ticks here).

    alpha_i = 1 - exp(-ln(2) * dt_i / halflife) so the smoothing is invariant
    to sampling cadence (sample_every doesn't change the effective time
    constant). NaN inputs are skipped (carry-forward).
    """
    n = len(values)
    out = np.empty(n, dtype=float)
    if n == 0:
        return out
    if halflife <= 0:
        return values.astype(float, copy=True)
    decay = math.log(2.0) / float(halflife)
    # Initialise with first finite value.
    init_idx = 0
    while init_idx < n and not np.isfinite(values[init_idx]):
        out[init_idx] = float("nan")
        init_idx += 1
    if init_idx >= n:
        return out
    out[init_idx] = float(values[init_idx])
    for i in range(init_idx + 1, n):
        dt = float(ts[i] - ts[i - 1])
        if dt < 0:
            dt = 0.0
        prev = out[i - 1]
        v = float(values[i])
        if not np.isfinite(prev):
            out[i] = v if np.isfinite(v) else float("nan")
            continue
        if not np.isfinite(v):
            out[i] = prev
            continue
        alpha = 1.0 - math.exp(-decay * dt)
        out[i] = prev + alpha * (v - prev)
    return out


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
    signal_detrend: bool = True,
    signal_halflife: int = 2_000,
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
    strike_arr = np.array(iv_df["strike"].to_list(), dtype=float)

    fit_mask = np.isin(strike_arr.astype(int), np.array(FIT_STRIKES, dtype=int))
    if int(np.sum(fit_mask)) < 3:
        return pl.DataFrame(), (0.0, 0.0, 0.0)
    x_fit, y_fit = x[fit_mask], y[fit_mask]

    a, b, c = np.polyfit(x_fit, y_fit, deg=2)

    if robust_refit and len(x_fit) >= 30:
        y_hat_1 = a * x_fit * x_fit + b * x_fit + c
        resid = y_fit - y_hat_1
        std = float(np.std(resid))
        if std > 0:
            keep = np.abs(resid) <= outlier_sigma * std
            if int(np.sum(keep)) >= 20:
                x2, y2 = x_fit[keep], y_fit[keep]
                a, b, c = np.polyfit(x2, y2, deg=2)

    iv_df = iv_df.sort(["strike", "global_ts"])
    x = np.array(iv_df["m_t"].to_list(), dtype=float)
    y = np.array(iv_df["iv"].to_list(), dtype=float)
    strike_arr = np.array(iv_df["strike"].to_list(), dtype=float)
    iv_hat = a * x * x + b * x + c
    iv_df = iv_df.with_columns(
        pl.lit(float(c)).alias("c_ema"),
        pl.lit(float(c)).alias("c_inst"),
    )
    a_used, b_used, c_used = float(a), float(b), float(c)

    spot = np.array(iv_df["underlying_mid"].to_list(), dtype=float)
    tte = np.array(iv_df["tte_days"].to_list(), dtype=float) / 365.0
    theo = _bs_call_price(spot=spot, strike=strike_arr, tte_years=tte, sigma=iv_hat, rate=rate)
    wall_mid = np.array(iv_df["wall_mid"].to_list(), dtype=float)

    delta_iv = y - iv_hat
    delta_px = wall_mid - theo

    iv_df = iv_df.with_columns([
        pl.Series("iv_hat", iv_hat.tolist()),
        pl.Series("delta_iv", delta_iv.tolist()),
        pl.Series("bs_fair", theo.tolist()),
        pl.Series("delta_px", delta_px.tolist()),
    ])

    if signal_detrend and signal_halflife > 0:
        # Per-strike short-window dt-aware EMA on the residual columns; then
        # signal_* = delta_* − ema_short(delta_*) removes any leftover
        # contract-specific bias / slow drift.
        iv_df = iv_df.sort(["strike", "global_ts"])
        pieces: list[pl.DataFrame] = []
        for (k_val,), sub in iv_df.group_by(["strike"], maintain_order=True):
            ts_k = sub["global_ts"].to_numpy().astype(float)
            d_iv = sub["delta_iv"].to_numpy().astype(float)
            d_px = sub["delta_px"].to_numpy().astype(float)
            d_iv_ema = _dt_ema(d_iv, ts_k, float(signal_halflife))
            d_px_ema = _dt_ema(d_px, ts_k, float(signal_halflife))
            sub = sub.with_columns([
                pl.Series("delta_iv_trend", d_iv_ema.tolist()),
                pl.Series("delta_px_trend", d_px_ema.tolist()),
            ])
            pieces.append(sub)
        iv_df = pl.concat(pieces).with_columns([
            (pl.col("delta_iv") - pl.col("delta_iv_trend")).alias("signal_iv"),
            (pl.col("delta_px") - pl.col("delta_px_trend")).alias("signal_px"),
        ])
    else:
        iv_df = iv_df.with_columns([
            pl.lit(0.0).alias("delta_iv_trend"),
            pl.lit(0.0).alias("delta_px_trend"),
            pl.col("delta_iv").alias("signal_iv"),
            pl.col("delta_px").alias("signal_px"),
        ])

    return iv_df.sort(["strike", "global_ts"]), (a_used, b_used, c_used)


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
    scale_by_sqrt_tte: bool = False,
    marker_opacity: float = 0.45,
    marker_size: int = 5,
    height: int = 620,
) -> go.Figure:
    """Figure 6a style: IV scatter + global quadratic fit.

    When scale_by_sqrt_tte is True, x-axis uses m_t = ln(K/S_t) / sqrt(TTE_t).
    When False, x-axis uses ln(K/S_t) directly.
    """
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
        fig.update_layout(title="IV smile (no valid points after filters)")
        return fig

    fig = go.Figure()
    strikes = sorted(df["strike"].unique().to_list())

    y_all = np.array(df["iv"].to_list(), dtype=float)
    x_m = np.array(df["m_t"].to_list(), dtype=float)
    if scale_by_sqrt_tte:
        x_all = x_m
        x_label = "m_t"
        x_title = "m_t = ln(K / S_t) / sqrt(TTE_t)"
        plot_title = "IV Smile: iv(m_t) with global quadratic fit"
    else:
        sqrt_t = np.sqrt(np.maximum(np.array(df["tte_days"].to_list(), dtype=float) / 365.0, 1e-12))
        x_all = x_m * sqrt_t
        x_label = "ln(K / S_t)"
        x_title = "ln(K / S_t)"
        plot_title = "IV Smile: iv(ln(K / S_t)) with global quadratic fit"

    strike_all = np.array(df["strike"].to_list(), dtype=float).astype(int)
    fit_mask_plot = np.isin(strike_all, np.array(FIT_STRIKES, dtype=int))
    a_plot, b_plot, c_plot = np.polyfit(x_all[fit_mask_plot], y_all[fit_mask_plot], deg=2)

    for k in strikes:
        sub = df.filter(pl.col("strike") == k)
        sub_x_m = np.array(sub["m_t"].to_list(), dtype=float)
        if scale_by_sqrt_tte:
            sub_x = sub_x_m
        else:
            sub_sqrt_t = np.sqrt(np.maximum(np.array(sub["tte_days"].to_list(), dtype=float) / 365.0, 1e-12))
            sub_x = sub_x_m * sub_sqrt_t
        fig.add_trace(
            go.Scatter(
                x=sub_x.tolist(),
                y=sub["iv"].to_list(),
                mode="markers",
                marker=dict(color=strike_color(int(k)), size=marker_size, opacity=marker_opacity),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                customdata=list(zip(sub["day"].to_list(), sub["timestamp"].to_list())),
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    f"{x_label}=%{{x:.4f}}<br>iv=%{{y:.2%}}<br>"
                    "day=%{customdata[0]}<br>ts=%{customdata[1]}<extra></extra>"
                ),
            )
        )

    x_grid = np.linspace(np.min(x_all), np.max(x_all), 400)
    y_grid = a_plot * x_grid * x_grid + b_plot * x_grid + c_plot
    fig.add_trace(
        go.Scatter(
            x=x_grid.tolist(),
            y=y_grid.tolist(),
            mode="lines",
            line=dict(color="black", width=3),
            name="Fitted parabola",
            hovertemplate=f"{x_label}=%{{x:.4f}}<br>iv_hat=%{{y:.2%}}<extra></extra>",
        )
    )

    fig.update_layout(
        height=height,
        title=f"{plot_title} | iv_hat = {a_plot:.4f}·x^2 + {b_plot:.4f}·x + {c_plot:.4f}",
        xaxis=dict(title=x_title),
        yaxis=dict(title="implied vol", tickformat=".2%"),
        hovermode="closest",
    )
    return fig


def plot_iv_smile_3d(
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
    scale_by_sqrt_tte: bool = False,
    y_axis: str = "tte_days",
    marker_opacity: float = 0.55,
    marker_size: int = 3,
    height: int = 700,
) -> go.Figure:
    """3D IV smile scatter.

    x-axis:
      - scale_by_sqrt_tte=True:  m_t = ln(K/S_t) / sqrt(TTE_t)
      - scale_by_sqrt_tte=False: ln(K/S_t)
    y-axis:
      - "tte_days": time-to-expiry in days
      - "global_ts": global timestamp
    z-axis: implied vol
    """
    if y_axis not in {"tte_days", "global_ts"}:
        raise ValueError("y_axis must be one of {'tte_days', 'global_ts'}")

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
        fig.update_layout(title="3D IV smile (no valid points after filters)")
        return fig

    x_m = np.array(df["m_t"].to_list(), dtype=float)
    if scale_by_sqrt_tte:
        x_all = x_m
        x_title = "m_t = ln(K / S_t) / sqrt(TTE_t)"
    else:
        sqrt_t = np.sqrt(np.maximum(np.array(df["tte_days"].to_list(), dtype=float) / 365.0, 1e-12))
        x_all = x_m * sqrt_t
        x_title = "ln(K / S_t)"

    fig = go.Figure()
    for k in sorted(df["strike"].unique().to_list()):
        sub = df.filter(pl.col("strike") == k)
        sub_x_m = np.array(sub["m_t"].to_list(), dtype=float)
        if scale_by_sqrt_tte:
            sub_x = sub_x_m
        else:
            sub_sqrt_t = np.sqrt(np.maximum(np.array(sub["tte_days"].to_list(), dtype=float) / 365.0, 1e-12))
            sub_x = sub_x_m * sub_sqrt_t

        sub_y = np.array(sub[y_axis].to_list(), dtype=float)
        sub_z = np.array(sub["iv"].to_list(), dtype=float)

        fig.add_trace(
            go.Scatter3d(
                x=sub_x.tolist(),
                y=sub_y.tolist(),
                z=sub_z.tolist(),
                mode="markers",
                marker=dict(
                    color=strike_color(int(k)),
                    size=marker_size,
                    opacity=marker_opacity,
                ),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                customdata=list(
                    zip(
                        sub["day"].to_list(),
                        sub["timestamp"].to_list(),
                        sub["tte_days"].to_list(),
                    )
                ),
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    "x=%{x:.4f}<br>"
                    "y=%{y:.2f}<br>"
                    "iv=%{z:.2%}<br>"
                    "day=%{customdata[0]}<br>"
                    "ts=%{customdata[1]}<br>"
                    "tte_days=%{customdata[2]:.2f}<extra></extra>"
                ),
            )
        )

    y_title = "TTE (days)" if y_axis == "tte_days" else "global_ts"
    fig.update_layout(
        height=height,
        title="3D IV Smile Scatter",
        scene=dict(
            xaxis_title=x_title,
            yaxis_title=y_title,
            zaxis_title="implied vol",
        ),
        legend_title_text="strike",
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
    signal: bool = False,
    subtract_self_ema: bool = True,
    height: int = 620,
) -> go.Figure:
    """Figure 6b style: delta_iv (or detrended signal_iv) over time by strike."""
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
        signal_detrend=signal,
        signal_halflife=_SIGNAL_HALFLIFE,
    )
    if df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="IV residual (no valid points after filters)")
        return fig

    y_col = "signal_iv" if signal else "delta_iv"
    base_title = "signal_iv = delta_iv − EMA_short(delta_iv)" if signal else "delta_iv = iv − iv_hat"
    title_extra = " | static c"

    plot_col = y_col
    if subtract_self_ema:
        plot_col = f"{y_col}_centered"
        df = df.sort(["strike", "global_ts"])
        pieces: list[pl.DataFrame] = []
        for (_k_val,), sub in df.group_by(["strike"], maintain_order=True):
            ts_k = sub["global_ts"].to_numpy().astype(float)
            v_k = sub[y_col].to_numpy().astype(float)
            slow = _dt_ema(v_k, ts_k, float(_SELF_EMA_HALFLIFE))
            sub = sub.with_columns(pl.Series(plot_col, (v_k - slow).tolist()))
            pieces.append(sub)
        df = pl.concat(pieces)
        title_extra += ", −self_EMA"
        title_y = f"{base_title}, then minus per-strike slow EMA"
    else:
        title_y = base_title

    fig = go.Figure()
    for k in sorted(df["strike"].unique().to_list()):
        sub = df.filter(pl.col("strike") == k)
        fig.add_trace(
            go.Scatter(
                x=sub["global_ts"].to_list(),
                y=sub[plot_col].to_list(),
                mode="lines",
                line=dict(color=strike_color(int(k)), width=1.2),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    f"{plot_col}=%{{y:.2%}}<br>global_ts=%{{x}}<extra></extra>"
                ),
            )
        )

    fig.add_hline(y=0.0, line=dict(color="rgba(0,0,0,0.35)", width=1, dash="dash"))
    fig.update_layout(
        height=height,
        title=f"IV residual over time: {title_y}{title_extra}",
        xaxis=dict(title="global_ts"),
        yaxis=dict(title=plot_col, tickformat=".2%"),
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
    signal: bool = False,
    subtract_self_ema: bool = True,
    height: int = 620,
) -> go.Figure:
    """Figure 6c style: delta_px (or detrended signal_px) over time by strike."""
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
        signal_detrend=signal,
        signal_halflife=_SIGNAL_HALFLIFE,
    )
    if df.is_empty():
        fig = go.Figure()
        fig.update_layout(title="Price residual (no valid points after filters)")
        return fig

    y_col = "signal_px" if signal else "delta_px"
    base_title = "signal_px = delta_px − EMA_short(delta_px)" if signal else "delta_px = wall_mid − BS_theo(iv_hat)"
    title_extra = " | static c"

    plot_col = y_col
    if subtract_self_ema:
        plot_col = f"{y_col}_centered"
        df = df.sort(["strike", "global_ts"])
        pieces: list[pl.DataFrame] = []
        for (_k_val,), sub in df.group_by(["strike"], maintain_order=True):
            ts_k = sub["global_ts"].to_numpy().astype(float)
            v_k = sub[y_col].to_numpy().astype(float)
            slow = _dt_ema(v_k, ts_k, float(_SELF_EMA_HALFLIFE))
            sub = sub.with_columns(pl.Series(plot_col, (v_k - slow).tolist()))
            pieces.append(sub)
        df = pl.concat(pieces)
        title_extra += ", −self_EMA"
        title_y = f"{base_title}, then minus per-strike slow EMA"
    else:
        title_y = base_title

    fig = go.Figure()
    for k in sorted(df["strike"].unique().to_list()):
        sub = df.filter(pl.col("strike") == k)
        fig.add_trace(
            go.Scatter(
                x=sub["global_ts"].to_list(),
                y=sub[plot_col].to_list(),
                mode="lines",
                line=dict(color=strike_color(int(k)), width=1.2),
                name=f"VEV_{int(k)}",
                legendgroup=f"VEV_{int(k)}",
                hovertemplate=(
                    f"<b>VEV_{int(k)}</b><br>"
                    f"{plot_col}=%{{y:.3f}}<br>global_ts=%{{x}}<extra></extra>"
                ),
            )
        )

    fig.add_hline(y=0.0, line=dict(color="rgba(0,0,0,0.35)", width=1, dash="dash"))
    fig.update_layout(
        height=height,
        title=f"Price residual over time: {title_y}{title_extra}",
        xaxis=dict(title="global_ts"),
        yaxis=dict(title=f"{plot_col} (SeaShells)"),
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
    max_lag: int = 200_000,
    n_lag_points: int = 200,
    n_sims: int = 300,
    seed: int = 0,
    series: str = "price",
    ts_per_step: int = 100,
    height: int = 560,
) -> go.Figure:
    """Figure 8 style: underlying autocorrelation vs Monte Carlo baseline.

    Lag is expressed in raw `timestamp` units (100 per row in this dataset).
    `max_lag` and the x-axis are in timestamps; we lay down `n_lag_points`
    log-spaced row-shifts and compute autocorr at each.

    series:
      - "price": raw mid_price level series. MC baseline is a random walk
        (cumsum of N(mu, sigma) increments matched to diff(p)) — the right
        null hypothesis for a price level. actual > baseline → momentum;
        actual < baseline → mean reversion.
      - "returns": log-return series diff(log(mid_price)). MC baseline is
        IID Gaussian white noise — the conventional null for return series.
    """
    if series not in {"returns", "price"}:
        raise ValueError("series must be one of {'returns', 'price'}")
    if ts_per_step <= 0:
        raise ValueError("ts_per_step must be positive")

    und = ctx.product_slice(UNDERLYING)
    if und.is_empty() or und.height < 200:
        fig = go.Figure()
        fig.update_layout(title="Underlying autocorrelation (insufficient data)")
        return fig

    p = np.array(und["mid_price"].to_list(), dtype=float)
    n_rows = p.size

    # Convert max_lag (timestamps) → row-shift, then build a log-spaced grid
    # so we don't burn 30k autocorr evals on near-identical lags.
    max_lag_rows = max(1, min(int(max_lag) // ts_per_step, n_rows - 20))
    if max_lag_rows < 2:
        fig = go.Figure()
        fig.update_layout(title="Underlying autocorrelation (max_lag too small)")
        return fig
    n_pts = max(2, min(int(n_lag_points), max_lag_rows))
    lags_rows = np.unique(
        np.round(np.geomspace(1, max_lag_rows, num=n_pts)).astype(int)
    )
    lags_ts = lags_rows * ts_per_step

    rng = np.random.default_rng(seed)

    if series == "returns":
        x = np.diff(np.log(np.maximum(p, 1e-9)))
        if x.size < max_lag_rows + 20:
            fig = go.Figure()
            fig.update_layout(title="Underlying autocorrelation (insufficient return length)")
            return fig
        actual = np.array([_autocorr_at_lag(x, int(k)) for k in lags_rows], dtype=float)
        sigma = float(np.std(x))
        baseline = np.empty((n_sims, lags_rows.size), dtype=float)
        for i in range(n_sims):
            noise = rng.normal(0.0, sigma, size=x.size)
            baseline[i, :] = np.array([_autocorr_at_lag(noise, int(k)) for k in lags_rows], dtype=float)
        title = (
            f"Underlying log-return autocorrelation vs IID Gaussian baseline "
            f"(max_lag={max_lag} ts, 1 step={ts_per_step} ts)"
        )
    else:
        actual = np.array([_autocorr_at_lag(p, int(k)) for k in lags_rows], dtype=float)
        increments = np.diff(p)
        mu_inc = float(np.mean(increments))
        sigma_inc = float(np.std(increments))
        n_inc = increments.size
        baseline = np.empty((n_sims, lags_rows.size), dtype=float)
        for i in range(n_sims):
            sim_inc = rng.normal(mu_inc, sigma_inc, size=n_inc)
            sim_p = np.empty(n_rows, dtype=float)
            sim_p[0] = p[0]
            sim_p[1:] = p[0] + np.cumsum(sim_inc)
            baseline[i, :] = np.array([_autocorr_at_lag(sim_p, int(k)) for k in lags_rows], dtype=float)
        title = (
            f"Underlying price autocorrelation vs random-walk baseline "
            f"(max_lag={max_lag} ts, 1 step={ts_per_step} ts)"
        )

    fig = go.Figure()
    for i in range(n_sims):
        fig.add_trace(
            go.Scatter(
                x=lags_ts.tolist(),
                y=baseline[i, :].tolist(),
                mode="lines",
                line=dict(color="rgba(0,0,0,0.12)", width=0.6),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    fig.add_trace(
        go.Scatter(
            x=lags_ts.tolist(),
            y=actual.tolist(),
            mode="lines",
            line=dict(color="red", width=2.2),
            name=UNDERLYING,
            hovertemplate="lag=%{x} ts<br>autocorr=%{y:.4f}<extra></extra>",
        )
    )

    fig.update_layout(
        height=height,
        title=title,
        xaxis=dict(title=f"lag (timestamps; 1 step = {ts_per_step} ts)", type="log"),
        yaxis=dict(title="autocorrelation"),
        hovermode="closest",
    )
    return fig
