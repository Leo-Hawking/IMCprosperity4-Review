"""Hyperparameter search for pepper_with_params.py.

Focus:
  - Primary axis: MIN_POSITION
  - Optional secondary axis: ASK_SIZE (default) for 2D plateau heatmap

Outputs:
  - CSV of all evaluated parameter points
  - Heatmap PNG for final PnL and plateau score
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

import numpy as np
import polars as pl

import old_verison_backtest


ROOT = Path(__file__).resolve().parent
DEFAULT_STRATEGY = ROOT / "pepper_with_params.py"
DEFAULT_OUTPUT_DIR = ROOT.parent / "analysis_outputs" / "pepper_hypersearch"
DEFAULT_PRODUCT = "INTARIAN_PEPPER_ROOT"


def _load_strategy_module(strategy_path: Path):
    spec = importlib.util.spec_from_file_location("pepper_strat", str(strategy_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load strategy from {strategy_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_one(
    strategy_path: Path,
    min_position: int,
    ask_size: int,
    product: str,
    days: list[int],
    round_num: int,
) -> dict:
    module = _load_strategy_module(strategy_path)

    # Inject hyperparameters before constructing Trader.
    module.MIN_POSITION = int(min_position)
    module.ASK_SIZE = int(ask_size)

    trader = module.Trader()
    sim = old_verison_backtest.simulate_multiday(
        trader,
        product=product,
        days=days,
        round_num=round_num,
        record_memory=False,
    )
    return sim["summary"]


def _plateau_score(pnl_grid: np.ndarray) -> np.ndarray:
    """Local-min score in 3x3 neighborhood. Higher means more robust plateau."""
    score = np.zeros_like(pnl_grid, dtype=float)
    n_row, n_col = pnl_grid.shape
    for r in range(n_row):
        for c in range(n_col):
            r0, r1 = max(0, r - 1), min(n_row, r + 2)
            c0, c1 = max(0, c - 1), min(n_col, c + 2)
            score[r, c] = np.min(pnl_grid[r0:r1, c0:c1])
    return score


def _to_int_seq(start: int, stop: int, step: int) -> list[int]:
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")
    return list(range(start, stop + 1, step))


def _plot_heatmaps(
    pnl_grid: np.ndarray,
    plateau_grid: np.ndarray,
    min_positions: list[int],
    ask_sizes: list[int],
    out_png: Path,
    title: str,
):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    extent = [min_positions[0], min_positions[-1], ask_sizes[0], ask_sizes[-1]]

    im0 = axes[0].imshow(
        pnl_grid,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[0].set_xlabel("MIN_POSITION")
    axes[0].set_ylabel("ASK_SIZE")
    axes[0].set_title("Final PnL (3-day)")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(np.argmax(pnl_grid), pnl_grid.shape)
    axes[0].plot(
        min_positions[best_idx[1]],
        ask_sizes[best_idx[0]],
        "k*",
        markersize=14,
        label=f"max={pnl_grid[best_idx]:.0f}",
    )
    axes[0].legend(loc="upper left")

    im1 = axes[1].imshow(
        plateau_grid,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[1].set_xlabel("MIN_POSITION")
    axes[1].set_ylabel("ASK_SIZE")
    axes[1].set_title("Plateau Score (3x3 local min)")
    plt.colorbar(im1, ax=axes[1])

    robust_idx = np.unravel_index(np.argmax(plateau_grid), plateau_grid.shape)
    axes[1].plot(
        min_positions[robust_idx[1]],
        ask_sizes[robust_idx[0]],
        "k*",
        markersize=14,
        label=f"best={plateau_grid[robust_idx]:.0f}",
    )
    axes[1].legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def run_search(
    strategy_path: Path,
    product: str,
    days: list[int],
    round_num: int,
    min_positions: list[int],
    ask_sizes: list[int],
    out_dir: Path,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    pnl_grid = np.zeros((len(ask_sizes), len(min_positions)), dtype=float)
    dd_grid = np.zeros_like(pnl_grid)

    total = len(min_positions) * len(ask_sizes)
    done = 0
    t0 = time.time()

    for i, ask_size in enumerate(ask_sizes):
        for j, min_pos in enumerate(min_positions):
            summary = _run_one(
                strategy_path=strategy_path,
                min_position=min_pos,
                ask_size=ask_size,
                product=product,
                days=days,
                round_num=round_num,
            )

            pnl = float(summary["final_pnl"])
            dd = float(summary["max_drawdown"])
            per_day = summary.get("per_day_pnl", [])

            pnl_grid[i, j] = pnl
            dd_grid[i, j] = dd

            rows.append(
                {
                    "min_position": min_pos,
                    "ask_size": ask_size,
                    "final_pnl": pnl,
                    "max_drawdown": dd,
                    "peak_pnl": float(summary.get("peak_pnl", 0.0)),
                    "trough_pnl": float(summary.get("trough_pnl", 0.0)),
                    "day_0_pnl": float(per_day[0]) if len(per_day) > 0 else None,
                    "day_1_pnl": float(per_day[1]) if len(per_day) > 1 else None,
                    "day_2_pnl": float(per_day[2]) if len(per_day) > 2 else None,
                }
            )

            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done)
            print(
                f"\r[{done:>3}/{total}] MIN_POSITION={min_pos:>3}, ASK_SIZE={ask_size:>2}, "
                f"PnL={pnl:>9.1f}, DD={dd:>8.1f}, ETA={eta:>6.1f}s",
                end="",
                flush=True,
            )

    print()

    plateau_grid = _plateau_score(pnl_grid)

    results_df = pl.DataFrame(rows).sort(["ask_size", "min_position"])
    csv_path = out_dir / "pepper_min_position_grid.csv"
    results_df.write_csv(csv_path)

    best_idx = np.unravel_index(np.argmax(pnl_grid), pnl_grid.shape)
    robust_idx = np.unravel_index(np.argmax(plateau_grid), plateau_grid.shape)

    heatmap_path = out_dir / "pepper_min_position_heatmap.png"
    _plot_heatmaps(
        pnl_grid=pnl_grid,
        plateau_grid=plateau_grid,
        min_positions=min_positions,
        ask_sizes=ask_sizes,
        out_png=heatmap_path,
        title=f"{product} | days={days} | round={round_num}",
    )

    summary = {
        "strategy": str(strategy_path),
        "product": product,
        "days": days,
        "round": round_num,
        "grid_shape": [len(ask_sizes), len(min_positions)],
        "best_pnl": {
            "min_position": min_positions[best_idx[1]],
            "ask_size": ask_sizes[best_idx[0]],
            "final_pnl": float(pnl_grid[best_idx]),
        },
        "best_plateau": {
            "min_position": min_positions[robust_idx[1]],
            "ask_size": ask_sizes[robust_idx[0]],
            "plateau_score": float(plateau_grid[robust_idx]),
            "final_pnl": float(pnl_grid[robust_idx]),
        },
        "output_csv": str(csv_path),
        "output_heatmap": str(heatmap_path),
    }

    summary_path = out_dir / "pepper_min_position_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    return summary


def main():
    parser = argparse.ArgumentParser(description="MIN_POSITION-focused hypersearch for pepper strategy")
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY), help="Path to strategy .py")
    parser.add_argument("--product", default=DEFAULT_PRODUCT)
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--days", type=int, nargs="+", default=[-2, -1, 0])

    parser.add_argument("--min-pos-start", type=int, default=55)
    parser.add_argument("--min-pos-stop", type=int, default=79)
    parser.add_argument("--min-pos-step", type=int, default=1)

    parser.add_argument("--ask-size-start", type=int, default=4)
    parser.add_argument("--ask-size-stop", type=int, default=12)
    parser.add_argument("--ask-size-step", type=int, default=1)

    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    out_dir = Path(args.outdir).resolve()

    min_positions = _to_int_seq(args.min_pos_start, args.min_pos_stop, args.min_pos_step)
    ask_sizes = _to_int_seq(args.ask_size_start, args.ask_size_stop, args.ask_size_step)

    summary = run_search(
        strategy_path=strategy_path,
        product=args.product,
        days=args.days,
        round_num=args.round,
        min_positions=min_positions,
        ask_sizes=ask_sizes,
        out_dir=out_dir,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
