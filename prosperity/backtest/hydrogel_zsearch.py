"""HYDROGEL_PACK 2D 超参数搜索：Z_OPEN × Z_CLOSE。

每个网格点：把策略源码里的 Z_OPEN/Z_CLOSE 常量替换掉 → 在 round 3 三天上跑回测
(--merge-pnl) → 解析 activity log 中 HYDROGEL_PACK 的 profit_and_loss 序列 →
记录最终 PnL 与 Sharpe（mean(ΔPnL)/std(ΔPnL)*sqrt(N)）。

输出：
  analysis_outputs/hydrogel_zsearch/grid.csv
  analysis_outputs/hydrogel_zsearch/heatmap_pnl.png
  analysis_outputs/hydrogel_zsearch/heatmap_sharpe.png
  analysis_outputs/hydrogel_zsearch/summary.json

用法:
  python backtest/hydrogel_zsearch.py
  python backtest/hydrogel_zsearch.py --jobs 4
  python backtest/hydrogel_zsearch.py --z-open-start 0.5 --z-open-stop 3.5 --z-open-step 0.25
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
DEFAULT_STRATEGY = ROOT / "round3trade" / "mean_reversion_simple.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "hydrogel_zsearch"
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
    # Replace `NAME = <expr>` (with optional inline comment) at line start.
    pattern = re.compile(
        rf"(?m)^([ \t]*{re.escape(name)}\s*=\s*)([^#\n]+)(.*)$"
    )
    val_str = f"{value:.6g}"
    new_content, n = pattern.subn(rf"\g<1>{val_str}\g<3>", content, count=1)
    if n == 0:
        raise ValueError(f"Constant not found: {name}")
    return new_content


def _build_temp_strategy(strategy_path: Path, z_open: float, z_close: float,
                         run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, "Z_OPEN", z_open)
    content = _patch_constant(content, "Z_CLOSE", z_close)
    tmp = run_dir / "strategy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _parse_pnl_series(log_path: Path, target: str) -> tuple[float, list[float]]:
    """Returns (final_pnl, list of profit_and_loss values per timestamp)."""
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


def _evaluate(strategy_path: Path, tmp_root: Path, z_open: float,
              z_close: float, task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    tmp_strategy = _build_temp_strategy(strategy_path, z_open, z_close, run_dir)
    log_path = run_dir / "bt.log"

    day_tokens = [_day_to_token(ROUND_NUM, d) for d in DAYS]
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
            f"Backtest failed z_open={z_open} z_close={z_close}\n"
            f"stderr={proc.stderr[-1500:]}"
        )

    final_pnl, series = _parse_pnl_series(log_path, TARGET_PRODUCT)
    sharpe = _compute_sharpe(series)
    return final_pnl, sharpe


def _plot_heatmap(z_opens: list[float], z_closes: list[float],
                  matrix: np.ndarray, title: str, out_path: Path,
                  cmap: str = "RdYlGn") -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6))
    im = ax.imshow(
        matrix, origin="lower", aspect="auto", cmap=cmap,
        extent=[z_opens[0], z_opens[-1], z_closes[0], z_closes[-1]],
    )
    ax.set_xlabel("Z_OPEN")
    ax.set_ylabel("Z_CLOSE")
    ax.set_title(title)
    plt.colorbar(im, ax=ax)

    # Annotate values
    n_close, n_open = matrix.shape
    if n_close * n_open <= 200:
        x_step = (z_opens[-1] - z_opens[0]) / max(1, n_open - 1)
        y_step = (z_closes[-1] - z_closes[0]) / max(1, n_close - 1)
        for i in range(n_close):
            for j in range(n_open):
                v = matrix[i, j]
                ax.text(
                    z_opens[0] + j * x_step,
                    z_closes[0] + i * y_step,
                    f"{v:.1f}" if abs(v) < 1000 else f"{v:.0f}",
                    ha="center", va="center", fontsize=7, color="black",
                )

    best_idx = np.unravel_index(np.nanargmax(matrix), matrix.shape)
    best_zo = z_opens[best_idx[1]]
    best_zc = z_closes[best_idx[0]]
    ax.plot(best_zo, best_zc, "k*", markersize=18,
            label=f"best={matrix[best_idx]:.2f} @ Z_OPEN={best_zo}, Z_CLOSE={best_zc}")
    ax.legend(loc="upper left")

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def run_search(strategy_path: Path, z_opens: list[float], z_closes: list[float],
               outdir: Path, jobs: int) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    n_open = len(z_opens)
    n_close = len(z_closes)
    pnl_mat = np.full((n_close, n_open), np.nan, dtype=float)
    sharpe_mat = np.full((n_close, n_open), np.nan, dtype=float)
    rows: list[dict] = []

    tasks = [
        (i, j, z_close, z_open)
        for i, z_close in enumerate(z_closes)
        for j, z_open in enumerate(z_opens)
    ]
    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="hydrogel_zsearch_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i: int, j: int, z_close: float, z_open: float,
                     pnl: float, sharpe: float) -> None:
            nonlocal done
            pnl_mat[i, j] = pnl
            sharpe_mat[i, j] = sharpe
            rows.append({
                "z_open": z_open, "z_close": z_close,
                "final_pnl": pnl, "sharpe": sharpe,
            })
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{done:>3}/{total}] z_open={z_open:>5.2f} "
                f"z_close={z_close:>5.2f}  PnL={pnl:>9.1f}  "
                f"Sharpe={sharpe:>6.3f}  ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, j, z_close, z_open in tasks:
                pnl, sharpe = _evaluate(
                    strategy_path, tmp_root, z_open, z_close,
                    task_id=f"r_{i}_{j}",
                )
                _consume(i, j, z_close, z_open, pnl, sharpe)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, j, z_close, z_open in tasks:
                    fut = ex.submit(
                        _evaluate, strategy_path, tmp_root,
                        z_open, z_close, f"r_{i}_{j}",
                    )
                    fut_map[fut] = (i, j, z_close, z_open)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, j, z_close, z_open = fut_map[fut]
                    pnl, sharpe = fut.result()
                    _consume(i, j, z_close, z_open, pnl, sharpe)

    print()

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["z_open", "z_close", "final_pnl", "sharpe"])
        w.writeheader()
        w.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(z_opens, z_closes, pnl_mat,
                  f"HYDROGEL_PACK final PnL | round={ROUND_NUM}, days={DAYS}",
                  pnl_png)
    _plot_heatmap(z_opens, z_closes, sharpe_mat,
                  f"HYDROGEL_PACK Sharpe | round={ROUND_NUM}, days={DAYS}",
                  sharpe_png)

    best_pnl_idx = np.unravel_index(np.nanargmax(pnl_mat), pnl_mat.shape)
    best_sharpe_idx = np.unravel_index(np.nanargmax(sharpe_mat), sharpe_mat.shape)

    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": ROUND_NUM,
        "days": DAYS,
        "grid_shape": {"z_open": n_open, "z_close": n_close},
        "best_pnl": {
            "z_open": z_opens[best_pnl_idx[1]],
            "z_close": z_closes[best_pnl_idx[0]],
            "final_pnl": float(pnl_mat[best_pnl_idx]),
            "sharpe": float(sharpe_mat[best_pnl_idx]),
        },
        "best_sharpe": {
            "z_open": z_opens[best_sharpe_idx[1]],
            "z_close": z_closes[best_sharpe_idx[0]],
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

    parser.add_argument("--z-open-start", type=float, default=0.5)
    parser.add_argument("--z-open-stop", type=float, default=3.0)
    parser.add_argument("--z-open-step", type=float, default=0.25)

    parser.add_argument("--z-close-start", type=float, default=0.0)
    parser.add_argument("--z-close-stop", type=float, default=2.0)
    parser.add_argument("--z-close-step", type=float, default=0.25)

    args = parser.parse_args()

    z_opens = _frange(args.z_open_start, args.z_open_stop, args.z_open_step)
    z_closes = _frange(args.z_close_start, args.z_close_stop, args.z_close_step)

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(strategy_path)

    summary = run_search(
        strategy_path=strategy_path,
        z_opens=z_opens,
        z_closes=z_closes,
        outdir=Path(args.outdir).resolve(),
        jobs=max(1, args.jobs),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
