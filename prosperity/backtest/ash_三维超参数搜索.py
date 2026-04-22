from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY = ROOT / "round1trade" / "new_ash_strategy.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "ash_hypersearch_new_r2_3d"
TARGET_PRODUCT = "ASH_COATED_OSMIUM"


def _day_to_token(round_num: int, day: int) -> str:
    if day < 0:
        return f"{round_num}--{abs(day)}"
    return f"{round_num}-{day}"


def _parse_range_spec(spec: str) -> list[float]:
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid range spec: {spec}. Expected start:stop:step")

    start, stop, step = float(parts[0]), float(parts[1]), float(parts[2])
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")

    values: list[float] = []
    x = start
    eps = step * 1e-6
    while x <= stop + eps:
        values.append(round(x, 10))
        x += step
    return values


def _value_str(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.10g}"


def _patch_constant(content: str, name: str, value: float) -> str:
    val_str = _value_str(value)

    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(f"{name}"):
            continue
        if "=" not in stripped:
            continue

        indent = line[: len(line) - len(stripped)]
        body = stripped

        if "#" in body:
            before_comment, comment = body.split("#", 1)
            comment = "#" + comment
        else:
            before_comment, comment = body, ""

        left, _ = before_comment.split("=", 1)
        newline = "\n" if line.endswith("\n") else ""
        if comment:
            lines[i] = f"{indent}{left}= {val_str} {comment.strip()}{newline}"
        else:
            lines[i] = f"{indent}{left}= {val_str}{newline}"
        return "".join(lines)

    raise ValueError(f"Constant not found: {name}")


def _build_temp_strategy(
    strategy_path: Path,
    k: float,
    k_take: float,
    k_quote: float,
    run_dir: Path,
) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, "K", k)
    content = _patch_constant(content, "K_take", k_take)
    content = _patch_constant(content, "K_quote", k_quote)

    temp_path = run_dir / "strategy_tmp.py"
    temp_path.write_text(content, encoding="utf-8")
    return temp_path


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


def _plateau_score_3d(pnl_cube: np.ndarray) -> np.ndarray:
    score = np.zeros_like(pnl_cube, dtype=float)
    a, b, c = pnl_cube.shape
    for i in range(a):
        for j in range(b):
            for k in range(c):
                i0, i1 = max(0, i - 1), min(a, i + 2)
                j0, j1 = max(0, j - 1), min(b, j + 2)
                k0, k1 = max(0, k - 1), min(c, k + 2)
                score[i, j, k] = float(np.min(pnl_cube[i0:i1, j0:j1, k0:k1]))
    return score


def _evaluate_one_point(
    strategy_path: Path,
    round_num: int,
    days: list[int],
    k: float,
    k_take: float,
    k_quote: float,
    run_dir: Path,
) -> float:
    temp_strategy = _build_temp_strategy(strategy_path, k, k_take, k_quote, run_dir)
    log_path = run_dir / "bt.log"

    day_tokens = [_day_to_token(round_num, d) for d in days]
    cmd = [
        "bash",
        "backtest/run_bt.sh",
        str(temp_strategy),
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
            f"Backtest failed for K={k}, K_take={k_take}, K_quote={k_quote}\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout={proc.stdout[-2000:]}\n"
            f"stderr={proc.stderr[-2000:]}"
        )

    return _parse_final_pnl(log_path, TARGET_PRODUCT)


def _plot_projection_heatmaps(
    k_vals: list[float],
    k_quote_vals: list[float],
    max_pnl_map: np.ndarray,
    max_plateau_map: np.ndarray,
    best_ktake_for_pnl: np.ndarray,
    best_ktake_for_plateau: np.ndarray,
    out_png: Path,
    title: str,
) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    extent = [k_vals[0], k_vals[-1], k_quote_vals[0], k_quote_vals[-1]]

    im0 = axes[0].imshow(
        max_pnl_map,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[0].set_xlabel("K")
    axes[0].set_ylabel("K_quote")
    axes[0].set_title("Best PnL over K_take")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(np.argmax(max_pnl_map), max_pnl_map.shape)
    axes[0].plot(
        k_vals[best_idx[1]],
        k_quote_vals[best_idx[0]],
        "k*",
        markersize=14,
        label=(
            f"PnL={max_pnl_map[best_idx]:.1f}, "
            f"K_take={best_ktake_for_pnl[best_idx]:.2f}"
        ),
    )
    axes[0].legend(loc="upper left")

    im1 = axes[1].imshow(
        max_plateau_map,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[1].set_xlabel("K")
    axes[1].set_ylabel("K_quote")
    axes[1].set_title("Best 3D Plateau over K_take")
    plt.colorbar(im1, ax=axes[1])

    robust_idx = np.unravel_index(np.argmax(max_plateau_map), max_plateau_map.shape)
    axes[1].plot(
        k_vals[robust_idx[1]],
        k_quote_vals[robust_idx[0]],
        "k*",
        markersize=14,
        label=(
            f"Plateau={max_plateau_map[robust_idx]:.1f}, "
            f"K_take={best_ktake_for_plateau[robust_idx]:.2f}"
        ),
    )
    axes[1].legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def run_search(
    strategy_path: Path,
    round_num: int,
    days: list[int],
    k_vals: list[float],
    k_take_vals: list[float],
    k_quote_vals: list[float],
    jobs: int,
    outdir: Path,
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    pnl_cube = np.zeros((len(k_quote_vals), len(k_vals), len(k_take_vals)), dtype=float)
    rows: list[dict] = []

    tasks: list[tuple[int, int, int, float, float, float]] = []
    for i, k_quote in enumerate(k_quote_vals):
        for j, k in enumerate(k_vals):
            for m, k_take in enumerate(k_take_vals):
                tasks.append((i, j, m, k, k_take, k_quote))

    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="ash_3d_hyper_") as tmpdir:
        tmp_root = Path(tmpdir)

        def worker(params: tuple[int, int, int, float, float, float]) -> tuple[int, int, int, float, float, float, float]:
            i, j, m, k, k_take, k_quote = params
            run_dir = tmp_root / f"p_{i}_{j}_{m}"
            run_dir.mkdir(parents=True, exist_ok=True)
            pnl = _evaluate_one_point(
                strategy_path=strategy_path,
                round_num=round_num,
                days=days,
                k=k,
                k_take=k_take,
                k_quote=k_quote,
                run_dir=run_dir,
            )
            return i, j, m, k, k_take, k_quote, pnl

        if jobs <= 1:
            for params in tasks:
                i, j, m, k, k_take, k_quote, pnl = worker(params)
                pnl_cube[i, j, m] = pnl
                rows.append(
                    {
                        "round": round_num,
                        "days": ",".join(str(d) for d in days),
                        "K": k,
                        "K_take": k_take,
                        "K_quote": k_quote,
                        "final_pnl": pnl,
                    }
                )
                done += 1
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done)
                print(
                    f"\r[{done:>4}/{total}] K={k:>5.2f} K_take={k_take:>5.2f} K_quote={k_quote:>5.2f} PnL={pnl:>10.1f} ETA={eta:>7.1f}s",
                    end="",
                    flush=True,
                )
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                future_map = {ex.submit(worker, p): p for p in tasks}
                for fut in concurrent.futures.as_completed(future_map):
                    i, j, m, k, k_take, k_quote, pnl = fut.result()
                    pnl_cube[i, j, m] = pnl
                    rows.append(
                        {
                            "round": round_num,
                            "days": ",".join(str(d) for d in days),
                            "K": k,
                            "K_take": k_take,
                            "K_quote": k_quote,
                            "final_pnl": pnl,
                        }
                    )
                    done += 1
                    elapsed = time.time() - t0
                    eta = elapsed / done * (total - done)
                    print(
                        f"\r[{done:>4}/{total}] K={k:>5.2f} K_take={k_take:>5.2f} K_quote={k_quote:>5.2f} PnL={pnl:>10.1f} ETA={eta:>7.1f}s",
                        end="",
                        flush=True,
                    )

    print()

    plateau_cube = _plateau_score_3d(pnl_cube)

    max_pnl_map = pnl_cube.max(axis=2)
    argmax_pnl_map = pnl_cube.argmax(axis=2)
    best_ktake_for_pnl = np.array([k_take_vals[idx] for idx in argmax_pnl_map.flat], dtype=float).reshape(argmax_pnl_map.shape)

    max_plateau_map = plateau_cube.max(axis=2)
    argmax_plateau_map = plateau_cube.argmax(axis=2)
    best_ktake_for_plateau = np.array([k_take_vals[idx] for idx in argmax_plateau_map.flat], dtype=float).reshape(argmax_plateau_map.shape)

    csv_path = outdir / "ash_3d_grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["round", "days", "K", "K_take", "K_quote", "final_pnl"],
        )
        writer.writeheader()
        writer.writerows(rows)

    heatmap_path = outdir / "ash_3d_projection_heatmap.png"
    _plot_projection_heatmaps(
        k_vals=k_vals,
        k_quote_vals=k_quote_vals,
        max_pnl_map=max_pnl_map,
        max_plateau_map=max_plateau_map,
        best_ktake_for_pnl=best_ktake_for_pnl,
        best_ktake_for_plateau=best_ktake_for_plateau,
        out_png=heatmap_path,
        title=f"ASH 3D Search | round={round_num}, days={days}",
    )

    best_idx = np.unravel_index(np.argmax(pnl_cube), pnl_cube.shape)
    robust_idx = np.unravel_index(np.argmax(plateau_cube), plateau_cube.shape)

    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": round_num,
        "days": days,
        "grid_shape": {
            "K_quote": len(k_quote_vals),
            "K": len(k_vals),
            "K_take": len(k_take_vals),
        },
        "best_pnl_point": {
            "K_quote": k_quote_vals[best_idx[0]],
            "K": k_vals[best_idx[1]],
            "K_take": k_take_vals[best_idx[2]],
            "final_pnl": float(pnl_cube[best_idx]),
        },
        "best_plateau_point": {
            "K_quote": k_quote_vals[robust_idx[0]],
            "K": k_vals[robust_idx[1]],
            "K_take": k_take_vals[robust_idx[2]],
            "plateau_score": float(plateau_cube[robust_idx]),
            "final_pnl": float(pnl_cube[robust_idx]),
        },
        "artifacts": {
            "grid_csv": str(csv_path),
            "projection_heatmap_png": str(heatmap_path),
        },
    }

    summary_path = outdir / "ash_3d_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="ASH 3D hyperparameter search for K, K_take, K_quote")
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY), help="Strategy .py path")
    parser.add_argument("--round", type=int, default=2)
    parser.add_argument("--days", type=int, nargs="+", default=[-1, 0, 1])

    parser.add_argument("--k-range", default="7:10:0.5", help="K range start:stop:step")
    parser.add_argument("--k-take-range", default="2:6:0.5", help="K_take range start:stop:step")
    parser.add_argument("--k-quote-range", default="6:10:0.5", help="K_quote range start:stop:step")

    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    k_vals = _parse_range_spec(args.k_range)
    k_take_vals = _parse_range_spec(args.k_take_range)
    k_quote_vals = _parse_range_spec(args.k_quote_range)

    outdir = Path(args.outdir).resolve()
    summary = run_search(
        strategy_path=strategy_path,
        round_num=args.round,
        days=args.days,
        k_vals=k_vals,
        k_take_vals=k_take_vals,
        k_quote_vals=k_quote_vals,
        jobs=max(1, args.jobs),
        outdir=outdir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
