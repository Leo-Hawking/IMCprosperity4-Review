"""
Hyperparameter grid search for trading strategies.
Focuses on finding flat high-PnL plateaus rather than sharp peaks.
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))


TUNABLE_PARAMS = {
    # v2 core
    "Z_SAT":           {"range": np.arange(0.5, 3.1, 0.25)},
    "DELTA_Q_TARGET":  {"range": np.arange(4, 25, 2)},
    "DELTA_Q_EXTREME": {"range": np.arange(30, 81, 5)},
    "BASE_MM_SIZE":    {"range": np.arange(2, 22, 2)},
    "INNER_ZONE":      {"range": np.arange(3, 9, 1)},
    # v3 OU estimation
    "SHRINK_MIN":      {"range": np.arange(0.1, 1.01, 0.1)},
    "SHRINK_MAX":      {"range": np.arange(1.0, 3.1, 0.25)},
    "EWMA_ALPHA":      {"range": np.arange(0.02, 0.32, 0.03)},
    "WARMUP_STEPS":    {"range": np.arange(500, 5001, 500)},
    "REFIT_INTERVAL":  {"range": np.arange(100, 1100, 100)},
    "R2_PRIOR":        {"range": np.arange(0.01, 0.16, 0.01)},
}

# module name used by _run_one; set via --strategy CLI or set_strategy()
_STRATEGY_MODULE: str = "新版ash"


def set_strategy(name: str):
    global _STRATEGY_MODULE
    _STRATEGY_MODULE = name


def _run_one(params: dict, product: str = "ASH_COATED_OSMIUM",
             days: list[int] | None = None) -> dict:
    import old_verison_backtest
    spec = importlib.util.spec_from_file_location(
        "strat", str(Path(__file__).parent / f"{_STRATEGY_MODULE}.py"))
    strat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strat)

    for k, v in params.items():
        setattr(strat, k, v)

    trader = strat.Trader()
    result = old_verison_backtest.simulate_multiday(trader, product, days=days)
    return result["summary"]


def grid_search_2d(param_x: str, param_y: str,
                   x_vals=None, y_vals=None,
                   fixed: dict | None = None,
                   product: str = "ASH_COATED_OSMIUM",
                   days: list[int] | None = None) -> dict:
    if x_vals is None:
        x_vals = TUNABLE_PARAMS[param_x]["range"]
    if y_vals is None:
        y_vals = TUNABLE_PARAMS[param_y]["range"]
    if fixed is None:
        fixed = {}

    x_vals = np.array(x_vals)
    y_vals = np.array(y_vals)
    pnl_grid = np.zeros((len(y_vals), len(x_vals)))
    dd_grid = np.zeros_like(pnl_grid)

    total = len(x_vals) * len(y_vals)
    t0 = time.time()

    for j, xv in enumerate(x_vals):
        for i, yv in enumerate(y_vals):
            params = dict(fixed)
            params[param_x] = float(xv)
            params[param_y] = float(yv)
            summary = _run_one(params, product, days)
            pnl_grid[i, j] = summary["final_pnl"]
            dd_grid[i, j] = summary["max_drawdown"]
            done = j * len(y_vals) + i + 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done)
            print(f"\r  [{done}/{total}] {param_x}={xv:.2f} {param_y}={yv:.2f}"
                  f"  PnL={summary['final_pnl']:.0f}  ETA={eta:.0f}s", end="", flush=True)

    print()
    return {
        "param_x": param_x, "param_y": param_y,
        "x_vals": x_vals, "y_vals": y_vals,
        "pnl": pnl_grid, "drawdown": dd_grid,
        "fixed": fixed,
    }


def plateau_score(pnl_grid: np.ndarray, sigma: float = 1.0) -> np.ndarray:
    from scipy.ndimage import uniform_filter, gaussian_filter
    smoothed = gaussian_filter(pnl_grid.astype(float), sigma=sigma)
    local_min = uniform_filter(pnl_grid.astype(float), size=3, mode="nearest")
    for i in range(pnl_grid.shape[0]):
        for j in range(pnl_grid.shape[1]):
            r0, r1 = max(0, i-1), min(pnl_grid.shape[0], i+2)
            c0, c1 = max(0, j-1), min(pnl_grid.shape[1], j+2)
            local_min[i, j] = pnl_grid[r0:r1, c0:c1].min()
    return local_min


def plot_heatmap(result: dict, mode: str = "pnl",
                 show: bool = True, savepath: str | None = None):
    import matplotlib.pyplot as plt

    pnl = result["pnl"]
    x_vals, y_vals = result["x_vals"], result["y_vals"]
    px, py = result["param_x"], result["param_y"]

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    im0 = axes[0].imshow(pnl, origin="lower", aspect="auto",
                         extent=[x_vals[0], x_vals[-1], y_vals[0], y_vals[-1]],
                         cmap="RdYlGn")
    axes[0].set_xlabel(px)
    axes[0].set_ylabel(py)
    axes[0].set_title("3-day PnL")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(pnl.argmax(), pnl.shape)
    axes[0].plot(x_vals[best_idx[1]], y_vals[best_idx[0]],
                 "k*", markersize=14, label=f"max={pnl.max():.0f}")
    axes[0].legend()

    ps = plateau_score(pnl)
    im1 = axes[1].imshow(ps, origin="lower", aspect="auto",
                         extent=[x_vals[0], x_vals[-1], y_vals[0], y_vals[-1]],
                         cmap="RdYlGn")
    axes[1].set_xlabel(px)
    axes[1].set_ylabel(py)
    axes[1].set_title("Plateau score (3x3 local min)")
    plt.colorbar(im1, ax=axes[1])

    best_p = np.unravel_index(ps.argmax(), ps.shape)
    axes[1].plot(x_vals[best_p[1]], y_vals[best_p[0]],
                 "k*", markersize=14, label=f"best={ps.max():.0f}")
    axes[1].legend()

    fixed_str = ", ".join(f"{k}={v}" for k, v in result.get("fixed", {}).items())
    if fixed_str:
        fig.suptitle(f"Fixed: {fixed_str}", fontsize=10)

    plt.tight_layout()
    if savepath:
        plt.savefig(savepath, dpi=120)
    if show:
        plt.show()
    return fig


SWEEP_PRESETS = {
    "v2": [
        ("Z_SAT", "BASE_MM_SIZE"),
        ("Z_SAT", "DELTA_Q_TARGET"),
        ("DELTA_Q_TARGET", "DELTA_Q_EXTREME"),
        ("BASE_MM_SIZE", "INNER_ZONE"),
    ],
    "v3": [
        ("Z_SAT", "REFIT_INTERVAL"),
        ("SHRINK_MIN", "SHRINK_MAX"),
        ("EWMA_ALPHA", "WARMUP_STEPS"),
        ("R2_PRIOR", "REFIT_INTERVAL"),
        ("Z_SAT", "BASE_MM_SIZE"),
    ],
}


def full_sweep(product: str = "ASH_COATED_OSMIUM",
               days: list[int] | None = None,
               savedir: str | None = None,
               preset: str = "v3"):
    import matplotlib
    matplotlib.use("Agg")

    pairs = SWEEP_PRESETS.get(preset, SWEEP_PRESETS["v3"])

    results = []
    for px, py in pairs:
        print(f"\n=== {px} vs {py} ===")
        r = grid_search_2d(px, py, product=product, days=days)
        results.append(r)

        ps = plateau_score(r["pnl"])
        best_p = np.unravel_index(ps.argmax(), ps.shape)
        print(f"  Plateau best: {px}={r['x_vals'][best_p[1]]:.2f}, "
              f"{py}={r['y_vals'][best_p[0]]:.2f}, "
              f"PnL={r['pnl'][best_p]:.0f}, plateau={ps.max():.0f}")

        if savedir:
            path = f"{savedir}/{px}_vs_{py}.png"
            plot_heatmap(r, show=False, savepath=path)
            print(f"  Saved: {path}")

    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="新版ash", help="Strategy module name (no .py)")
    ap.add_argument("--param-x", default="Z_SAT")
    ap.add_argument("--param-y", default="REFIT_INTERVAL")
    ap.add_argument("--sweep", action="store_true", help="Run preset sweep pairs")
    ap.add_argument("--preset", default="v3", choices=list(SWEEP_PRESETS.keys()))
    ap.add_argument("--savedir", default=str(Path(__file__).parent.parent / "analysis_outputs"))
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    set_strategy(args.strategy)
    Path(args.savedir).mkdir(parents=True, exist_ok=True)

    if args.sweep:
        full_sweep(savedir=args.savedir, preset=args.preset)
    else:
        print(f"\n=== {args.param_x} vs {args.param_y} ({args.strategy}) ===")
        r = grid_search_2d(args.param_x, args.param_y)
        plot_heatmap(r, show=args.show, savepath=f"{args.savedir}/{args.param_x}_vs_{args.param_y}.png")
