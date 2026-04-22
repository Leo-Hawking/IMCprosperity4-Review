from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY = ROOT / "round1trade" / "new_ash_strategy.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "ash_hypersearch_new"
TARGET_PRODUCT = "ASH_COATED_OSMIUM"


def _day_to_token(round_num: int, day: int) -> str:
    if day < 0:
        return f"{round_num}--{abs(day)}"
    return f"{round_num}-{day}"


def _parse_range_spec(spec: str) -> list[float]:
    # format: start:stop:step (inclusive stop with tolerance)
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid range spec: {spec}. Expected start:stop:step")

    start, stop, step = float(parts[0]), float(parts[1]), float(parts[2])
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")

    vals: list[float] = []
    x = start
    eps = step * 1e-6
    while x <= stop + eps:
        vals.append(round(x, 10))
        x += step
    return vals


def _patch_constant(content: str, name: str, value: float) -> str:
    # Replace first top-level assignment line like: NAME = xxx  # comment
    if float(value).is_integer():
        val_str = str(int(value))
    else:
        val_str = f"{value:.10g}"

    lines = content.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(f"{name}"):
            continue
        if "=" not in stripped:
            continue

        # Keep indentation and trailing inline comment/newline exactly as-is.
        indent_len = len(line) - len(stripped)
        indent = line[:indent_len]
        body = stripped

        if "#" in body:
            before_comment, comment = body.split("#", 1)
            comment = "#" + comment
        else:
            before_comment, comment = body, ""

        if "=" not in before_comment:
            continue

        left, _right = before_comment.split("=", 1)
        newline = "\n" if line.endswith("\n") else ""
        lines[idx] = f"{indent}{left}= {val_str}{(' ' + comment.strip()) if comment else ''}{newline}"
        return "".join(lines)

    raise ValueError(f"Constant not found: {name}")


def _build_temp_strategy(strategy_path: Path, x_name: str, x_val: float, y_name: str, y_val: float, run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, x_name, x_val)
    content = _patch_constant(content, y_name, y_val)
    tmp_strategy = run_dir / "strategy_tmp.py"
    tmp_strategy.write_text(content, encoding="utf-8")
    return tmp_strategy


def _parse_final_pnl(log_path: Path, target_product: str) -> float:
    header = None
    pnl_col = None
    product_col = None
    last_pnl = None

    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if header is None and line.startswith("day;") and "profit_and_loss" in line:
                header = line.split(";")
                product_col = header.index("product")
                pnl_col = header.index("profit_and_loss")
                continue

            if header is None or ";" not in line:
                continue

            parts = line.split(";")
            if len(parts) <= max(product_col, pnl_col):
                continue
            if parts[product_col] != target_product:
                continue
            try:
                last_pnl = float(parts[pnl_col])
            except ValueError:
                continue

    if last_pnl is None:
        raise RuntimeError(f"Could not parse final PnL for {target_product} from {log_path}")
    return last_pnl


def _plateau_score_2d(pnl_grid: np.ndarray) -> np.ndarray:
    # Plateau score: local minimum in 3x3 neighborhood.
    score = np.zeros_like(pnl_grid, dtype=float)
    r_n, c_n = pnl_grid.shape
    for r in range(r_n):
        for c in range(c_n):
            r0, r1 = max(0, r - 1), min(r_n, r + 2)
            c0, c1 = max(0, c - 1), min(c_n, c + 2)
            score[r, c] = float(np.min(pnl_grid[r0:r1, c0:c1]))
    return score


def _plot_heatmap(x_vals: list[float], y_vals: list[float], pnl_grid: np.ndarray, plateau_grid: np.ndarray, x_name: str, y_name: str, title: str, out_png: Path) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    extent = [x_vals[0], x_vals[-1], y_vals[0], y_vals[-1]]

    im0 = axes[0].imshow(
        pnl_grid,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[0].set_xlabel(x_name)
    axes[0].set_ylabel(y_name)
    axes[0].set_title("Final PnL")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(np.argmax(pnl_grid), pnl_grid.shape)
    axes[0].plot(
        x_vals[best_idx[1]],
        y_vals[best_idx[0]],
        "k*",
        markersize=14,
        label=f"max={pnl_grid[best_idx]:.1f}",
    )
    axes[0].legend(loc="upper left")

    im1 = axes[1].imshow(
        plateau_grid,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[1].set_xlabel(x_name)
    axes[1].set_ylabel(y_name)
    axes[1].set_title("Plateau Score (3x3 local min)")
    plt.colorbar(im1, ax=axes[1])

    robust_idx = np.unravel_index(np.argmax(plateau_grid), plateau_grid.shape)
    axes[1].plot(
        x_vals[robust_idx[1]],
        y_vals[robust_idx[0]],
        "k*",
        markersize=14,
        label=f"best={plateau_grid[robust_idx]:.1f}",
    )
    axes[1].legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def _evaluate_one_point(strategy_path: Path, round_num: int, days: list[int], x_name: str, x_val: float, y_name: str, y_val: float, task_id: str, tmp_root: Path) -> float:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    tmp_strategy = _build_temp_strategy(strategy_path, x_name, x_val, y_name, y_val, run_dir)
    log_path = run_dir / "bt.log"

    day_tokens = [_day_to_token(round_num, d) for d in days]
    cmd = [
        "bash",
        "backtest/run_bt.sh",
        str(tmp_strategy),
        *day_tokens,
        "--merge-pnl",
        "--out",
        str(log_path),
    ]

    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Backtest failed for {x_name}={x_val}, {y_name}={y_val}\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout={proc.stdout[-2000:]}\n"
            f"stderr={proc.stderr[-2000:]}"
        )

    return _parse_final_pnl(log_path, TARGET_PRODUCT)


def _run_grid(strategy_path: Path, round_num: int, days: list[int], x_name: str, x_vals: list[float], y_name: str, y_vals: list[float], jobs: int):
    pnl_grid = np.zeros((len(y_vals), len(x_vals)), dtype=float)
    rows: list[dict] = []

    tasks: list[tuple[int, int, float, float, str]] = []
    for i, yv in enumerate(y_vals):
        for j, xv in enumerate(x_vals):
            tasks.append((i, j, xv, yv, f"p_{i}_{j}"))

    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="ash_hyper_") as tmpdir:
        tmp_root = Path(tmpdir)

        if jobs <= 1:
            for i, j, xv, yv, task_id in tasks:
                pnl = _evaluate_one_point(strategy_path, round_num, days, x_name, xv, y_name, yv, task_id, tmp_root)
                pnl_grid[i, j] = pnl
                rows.append({"round": round_num, "days": ",".join(str(d) for d in days), x_name: xv, y_name: yv, "final_pnl": pnl})
                done += 1
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done)
                print(f"\r[{done:>4}/{total}] {x_name}={xv:>7.3f} {y_name}={yv:>7.3f} PnL={pnl:>10.1f} ETA={eta:>7.1f}s", end="", flush=True)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                futures: dict[concurrent.futures.Future, tuple[int, int, float, float]] = {}
                for i, j, xv, yv, task_id in tasks:
                    fut = ex.submit(_evaluate_one_point, strategy_path, round_num, days, x_name, xv, y_name, yv, task_id, tmp_root)
                    futures[fut] = (i, j, xv, yv)

                for fut in concurrent.futures.as_completed(futures):
                    i, j, xv, yv = futures[fut]
                    pnl = fut.result()
                    pnl_grid[i, j] = pnl
                    rows.append({"round": round_num, "days": ",".join(str(d) for d in days), x_name: xv, y_name: yv, "final_pnl": pnl})
                    done += 1
                    elapsed = time.time() - t0
                    eta = elapsed / done * (total - done)
                    print(f"\r[{done:>4}/{total}] {x_name}={xv:>7.3f} {y_name}={yv:>7.3f} PnL={pnl:>10.1f} ETA={eta:>7.1f}s", end="", flush=True)

    print()
    return pnl_grid, rows


def _write_grid_csv(out_csv: Path, rows: list[dict], x_name: str, y_name: str) -> None:
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["round", "days", x_name, y_name, "final_pnl"])
        w.writeheader()
        w.writerows(rows)


def _fine_range_from_center(center: float, radius: float, step: float) -> list[float]:
    start = center - radius
    stop = center + radius
    vals: list[float] = []
    x = start
    eps = step * 1e-6
    while x <= stop + eps:
        vals.append(round(x, 10))
        x += step
    # unique and sorted, avoid precision duplicates
    return sorted(set(vals))


def run_two_stage(
    strategy_path: Path,
    outdir: Path,
    round_num: int,
    days: list[int],
    x_name: str,
    y_name: str,
    coarse_x: list[float],
    coarse_y: list[float],
    fine_x_radius: float,
    fine_y_radius: float,
    fine_x_step: float,
    fine_y_step: float,
    jobs: int,
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    # Stage 1: coarse grid
    coarse_pnl, coarse_rows = _run_grid(
        strategy_path=strategy_path,
        round_num=round_num,
        days=days,
        x_name=x_name,
        x_vals=coarse_x,
        y_name=y_name,
        y_vals=coarse_y,
        jobs=jobs,
    )
    coarse_plateau = _plateau_score_2d(coarse_pnl)

    coarse_best_idx = np.unravel_index(np.argmax(coarse_pnl), coarse_pnl.shape)
    coarse_robust_idx = np.unravel_index(np.argmax(coarse_plateau), coarse_plateau.shape)
    coarse_best = {
        x_name: coarse_x[coarse_best_idx[1]],
        y_name: coarse_y[coarse_best_idx[0]],
        "final_pnl": float(coarse_pnl[coarse_best_idx]),
    }
    coarse_robust = {
        x_name: coarse_x[coarse_robust_idx[1]],
        y_name: coarse_y[coarse_robust_idx[0]],
        "plateau_score": float(coarse_plateau[coarse_robust_idx]),
        "final_pnl": float(coarse_pnl[coarse_robust_idx]),
    }

    _write_grid_csv(outdir / "coarse_grid.csv", coarse_rows, x_name, y_name)
    _plot_heatmap(
        x_vals=coarse_x,
        y_vals=coarse_y,
        pnl_grid=coarse_pnl,
        plateau_grid=coarse_plateau,
        x_name=x_name,
        y_name=y_name,
        title=f"Coarse | round={round_num}, days={days}",
        out_png=outdir / "coarse_heatmap.png",
    )

    # Stage 2: fine grid centered at coarse robust point
    center_x = float(coarse_robust[x_name])
    center_y = float(coarse_robust[y_name])
    fine_x = _fine_range_from_center(center_x, fine_x_radius, fine_x_step)
    fine_y = _fine_range_from_center(center_y, fine_y_radius, fine_y_step)

    fine_pnl, fine_rows = _run_grid(
        strategy_path=strategy_path,
        round_num=round_num,
        days=days,
        x_name=x_name,
        x_vals=fine_x,
        y_name=y_name,
        y_vals=fine_y,
        jobs=jobs,
    )
    fine_plateau = _plateau_score_2d(fine_pnl)

    fine_best_idx = np.unravel_index(np.argmax(fine_pnl), fine_pnl.shape)
    fine_robust_idx = np.unravel_index(np.argmax(fine_plateau), fine_plateau.shape)
    fine_best = {
        x_name: fine_x[fine_best_idx[1]],
        y_name: fine_y[fine_best_idx[0]],
        "final_pnl": float(fine_pnl[fine_best_idx]),
    }
    fine_robust = {
        x_name: fine_x[fine_robust_idx[1]],
        y_name: fine_y[fine_robust_idx[0]],
        "plateau_score": float(fine_plateau[fine_robust_idx]),
        "final_pnl": float(fine_pnl[fine_robust_idx]),
    }

    _write_grid_csv(outdir / "fine_grid.csv", fine_rows, x_name, y_name)
    _plot_heatmap(
        x_vals=fine_x,
        y_vals=fine_y,
        pnl_grid=fine_pnl,
        plateau_grid=fine_plateau,
        x_name=x_name,
        y_name=y_name,
        title=f"Fine | round={round_num}, days={days}",
        out_png=outdir / "fine_heatmap.png",
    )

    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": round_num,
        "days": days,
        "params": [x_name, y_name],
        "coarse": {
            "x_vals": coarse_x,
            "y_vals": coarse_y,
            "best_pnl_point": coarse_best,
            "best_plateau_point": coarse_robust,
            "grid_csv": str(outdir / "coarse_grid.csv"),
            "heatmap_png": str(outdir / "coarse_heatmap.png"),
        },
        "fine": {
            "center_from_coarse_plateau": {x_name: center_x, y_name: center_y},
            "x_vals": fine_x,
            "y_vals": fine_y,
            "best_pnl_point": fine_best,
            "best_plateau_point": fine_robust,
            "grid_csv": str(outdir / "fine_grid.csv"),
            "heatmap_png": str(outdir / "fine_heatmap.png"),
        },
    }

    summary_path = outdir / "two_stage_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-stage hyperparameter search (coarse -> fine) for new_ash_strategy")
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY), help="Path to strategy .py")
    parser.add_argument("--round", type=int, default=2)
    parser.add_argument("--days", type=int, nargs="+", default=[-1, 0, 1])
    parser.add_argument("--param-x", default="K")
    parser.add_argument("--param-y", default="VOL_THRESHOLD")
    parser.add_argument("--coarse-x", default="3:9:1", help="start:stop:step")
    parser.add_argument("--coarse-y", default="10:40:5", help="start:stop:step")
    parser.add_argument("--fine-x-radius", type=float, default=2.0)
    parser.add_argument("--fine-y-radius", type=float, default=10.0)
    parser.add_argument("--fine-x-step", type=float, default=0.25)
    parser.add_argument("--fine-y-step", type=float, default=2.0)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    coarse_x = _parse_range_spec(args.coarse_x)
    coarse_y = _parse_range_spec(args.coarse_y)

    outdir = Path(args.outdir).resolve()
    summary = run_two_stage(
        strategy_path=strategy_path,
        outdir=outdir,
        round_num=args.round,
        days=args.days,
        x_name=args.param_x,
        y_name=args.param_y,
        coarse_x=coarse_x,
        coarse_y=coarse_y,
        fine_x_radius=args.fine_x_radius,
        fine_y_radius=args.fine_y_radius,
        fine_x_step=args.fine_x_step,
        fine_y_step=args.fine_y_step,
        jobs=max(1, args.jobs),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
