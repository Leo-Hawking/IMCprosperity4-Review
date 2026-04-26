"""HYDROGEL_PACK 2D 超参数搜索：D × W。

D = 价格 / 仓位刻度（mid-MU=D 时 target 打满 ±POS_MAX）
W = 调仓最小步长（手）

每个网格点：patch 策略源码里的 D / W → 在 round 3 三天上跑回测 (--merge-pnl) →
解析 HYDROGEL_PACK 的 profit_and_loss 序列 → 记录最终 PnL 与 Sharpe。

输出：
  analysis_outputs/hydrogel_dwsearch/grid.csv
  analysis_outputs/hydrogel_dwsearch/heatmap_pnl.png
  analysis_outputs/hydrogel_dwsearch/heatmap_sharpe.png
  analysis_outputs/hydrogel_dwsearch/summary.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY = ROOT / "round3trade" / "mean_reversion_pos.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "hydrogel_dwsearch"
TARGET_PRODUCT = "HYDROGEL_PACK"
ROUND_NUM = 3
DAYS = [0, 1, 2]


def _frange(start: float, stop: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")
    out: list[float] = []
    x = start
    eps = step * 1e-6
    while x <= stop + eps:
        out.append(round(x, 10))
        x += step
    return out


def _day_to_token(round_num: int, day: int) -> str:
    if day < 0:
        return f"{round_num}--{abs(day)}"
    return f"{round_num}-{day}"


def _patch_constant(content: str, name: str, value: float) -> str:
    pattern = re.compile(
        rf"(?m)^([ \t]*{re.escape(name)}\s*=\s*)([^#\n]+)(.*)$"
    )
    val_str = f"{value:.6g}"
    new_content, n = pattern.subn(rf"\g<1>{val_str}\g<3>", content, count=1)
    if n == 0:
        raise ValueError(f"Constant not found: {name}")
    return new_content


def _build_temp_strategy(strategy_path: Path, d: float, w: float,
                         run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, "D", d)
    content = _patch_constant(content, "W", w)
    tmp = run_dir / "strategy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _parse_pnl_series(log_path: Path, target: str) -> tuple[float, list[float]]:
    header_idx = {"product": None, "pnl": None}
    series: list[float] = []
    final_pnl: float | None = None

    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("day;") and "profit_and_loss" in line:
                cols = line.split(";")
                header_idx["product"] = cols.index("product")
                header_idx["pnl"] = cols.index("profit_and_loss")
                continue
            if header_idx["product"] is None or ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) <= max(header_idx["product"], header_idx["pnl"]):
                continue
            if parts[header_idx["product"]] != target:
                continue
            try:
                v = float(parts[header_idx["pnl"]])
            except ValueError:
                continue
            series.append(v)
            final_pnl = v

    if final_pnl is None:
        raise RuntimeError(f"No PnL rows for {target} in {log_path}")
    return final_pnl, series


def _compute_sharpe(pnl_series: list[float]) -> float:
    if len(pnl_series) < 3:
        return 0.0
    arr = np.asarray(pnl_series, dtype=float)
    diffs = np.diff(arr)
    mu = float(np.mean(diffs))
    sd = float(np.std(diffs, ddof=1))
    if sd <= 1e-12:
        return 0.0
    return mu / sd * math.sqrt(len(diffs))


def _evaluate(strategy_path: Path, tmp_root: Path, d: float, w: float,
              task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    tmp_strategy = _build_temp_strategy(strategy_path, d, w, run_dir)
    log_path = run_dir / "bt.log"

    day_tokens = [_day_to_token(ROUND_NUM, d_) for d_ in DAYS]
    cmd = [
        "bash", "backtest/run_bt.sh", str(tmp_strategy),
        *day_tokens, "--merge-pnl", "--out", str(log_path),
    ]
    proc = subprocess.run(
        cmd, cwd=str(ROOT), text=True, capture_output=True,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Backtest failed D={d} W={w}\nstderr={proc.stderr[-1500:]}"
        )

    final_pnl, series = _parse_pnl_series(log_path, TARGET_PRODUCT)
    sharpe = _compute_sharpe(series)
    return final_pnl, sharpe


def _plot_heatmap(d_vals: list[float], w_vals: list[float],
                  matrix: np.ndarray, title: str, out_path: Path,
                  cmap: str = "RdYlGn") -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 7))
    im = ax.imshow(
        matrix, origin="lower", aspect="auto", cmap=cmap,
        extent=[d_vals[0], d_vals[-1], w_vals[0], w_vals[-1]],
    )
    ax.set_xlabel("D  (price scale)")
    ax.set_ylabel("W  (rebalance threshold, lots)")
    ax.set_title(title)
    plt.colorbar(im, ax=ax)

    n_w, n_d = matrix.shape
    if n_w * n_d <= 200:
        x_step = (d_vals[-1] - d_vals[0]) / max(1, n_d - 1)
        y_step = (w_vals[-1] - w_vals[0]) / max(1, n_w - 1)
        for i in range(n_w):
            for j in range(n_d):
                v = matrix[i, j]
                ax.text(
                    d_vals[0] + j * x_step,
                    w_vals[0] + i * y_step,
                    f"{v:.1f}" if abs(v) < 1000 else f"{v:.0f}",
                    ha="center", va="center", fontsize=7, color="black",
                )

    best_idx = np.unravel_index(np.nanargmax(matrix), matrix.shape)
    best_d = d_vals[best_idx[1]]
    best_w = w_vals[best_idx[0]]
    ax.plot(best_d, best_w, "k*", markersize=18,
            label=f"best={matrix[best_idx]:.2f} @ D={best_d}, W={best_w}")
    ax.legend(loc="upper left")

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def run_search(strategy_path: Path, d_vals: list[float], w_vals: list[float],
               outdir: Path, jobs: int) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    n_d = len(d_vals)
    n_w = len(w_vals)
    pnl_mat = np.full((n_w, n_d), np.nan, dtype=float)
    sharpe_mat = np.full((n_w, n_d), np.nan, dtype=float)
    rows: list[dict] = []

    tasks = [
        (i, j, w, d)
        for i, w in enumerate(w_vals)
        for j, d in enumerate(d_vals)
    ]
    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="hydrogel_dwsearch_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i: int, j: int, w: float, d: float,
                     pnl: float, sharpe: float) -> None:
            nonlocal done
            pnl_mat[i, j] = pnl
            sharpe_mat[i, j] = sharpe
            rows.append({"D": d, "W": w, "final_pnl": pnl, "sharpe": sharpe})
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{done:>3}/{total}] D={d:>6.1f} W={w:>4.0f}  "
                f"PnL={pnl:>10.1f}  Sharpe={sharpe:>7.3f}  ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, j, w, d in tasks:
                pnl, sharpe = _evaluate(strategy_path, tmp_root, d, w,
                                        task_id=f"r_{i}_{j}")
                _consume(i, j, w, d, pnl, sharpe)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, j, w, d in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root, d, w,
                                    f"r_{i}_{j}")
                    fut_map[fut] = (i, j, w, d)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, j, w, d = fut_map[fut]
                    pnl, sharpe = fut.result()
                    _consume(i, j, w, d, pnl, sharpe)

    print()

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        wcsv = csv.DictWriter(f, fieldnames=["D", "W", "final_pnl", "sharpe"])
        wcsv.writeheader()
        wcsv.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(d_vals, w_vals, pnl_mat,
                  f"HYDROGEL_PACK final PnL | round={ROUND_NUM}, days={DAYS}",
                  pnl_png)
    _plot_heatmap(d_vals, w_vals, sharpe_mat,
                  f"HYDROGEL_PACK Sharpe | round={ROUND_NUM}, days={DAYS}",
                  sharpe_png)

    best_pnl_idx = np.unravel_index(np.nanargmax(pnl_mat), pnl_mat.shape)
    best_sharpe_idx = np.unravel_index(np.nanargmax(sharpe_mat), sharpe_mat.shape)

    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": ROUND_NUM,
        "days": DAYS,
        "grid_shape": {"D": n_d, "W": n_w},
        "best_pnl": {
            "D": d_vals[best_pnl_idx[1]],
            "W": w_vals[best_pnl_idx[0]],
            "final_pnl": float(pnl_mat[best_pnl_idx]),
            "sharpe": float(sharpe_mat[best_pnl_idx]),
        },
        "best_sharpe": {
            "D": d_vals[best_sharpe_idx[1]],
            "W": w_vals[best_sharpe_idx[0]],
            "final_pnl": float(pnl_mat[best_sharpe_idx]),
            "sharpe": float(sharpe_mat[best_sharpe_idx]),
        },
        "artifacts": {
            "grid_csv": str(csv_path),
            "heatmap_pnl": str(pnl_png),
            "heatmap_sharpe": str(sharpe_png),
        },
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY))
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    parser.add_argument("--jobs", type=int, default=1)

    parser.add_argument("--d-start", type=float, default=20.0)
    parser.add_argument("--d-stop", type=float, default=200.0)
    parser.add_argument("--d-step", type=float, default=20.0)

    parser.add_argument("--w-start", type=float, default=2.0)
    parser.add_argument("--w-stop", type=float, default=40.0)
    parser.add_argument("--w-step", type=float, default=4.0)

    args = parser.parse_args()

    d_vals = _frange(args.d_start, args.d_stop, args.d_step)
    w_vals = _frange(args.w_start, args.w_stop, args.w_step)

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(strategy_path)

    summary = run_search(
        strategy_path=strategy_path,
        d_vals=d_vals, w_vals=w_vals,
        outdir=Path(args.outdir).resolve(),
        jobs=max(1, args.jobs),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
