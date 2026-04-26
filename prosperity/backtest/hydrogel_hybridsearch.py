"""HYDROGEL_PACK 3D 超参数搜索：SPAN × D × W。

每个网格点：patch 策略源码里的 SPAN / D / W → round 3 三天 --merge-pnl 回测 →
解析 HYDROGEL_PACK profit_and_loss → 记录最终 PnL 与 Sharpe。

输出：
  analysis_outputs/hydrogel_hybridsearch/grid.csv
  analysis_outputs/hydrogel_hybridsearch/heatmap_pnl.png      # 每个 SPAN 一格
  analysis_outputs/hydrogel_hybridsearch/heatmap_sharpe.png   # 每个 SPAN 一格
  analysis_outputs/hydrogel_hybridsearch/summary.json
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
DEFAULT_STRATEGY = ROOT / "round3trade" / "mean_reversion_hybrid.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "hydrogel_hybridsearch"
TARGET_PRODUCT = "HYDROGEL_PACK"
ROUND_NUM = 3
DAYS = [0, 1, 2]


def _parse_value_list(spec: str) -> list[float]:
    """逗号分隔, 例: '200,1000,3000,10000'"""
    return [float(x) for x in spec.split(",") if x.strip()]


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


def _build_temp_strategy(strategy_path: Path, span: float, d: float, w: float,
                         run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, "SPAN", span)
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


def _evaluate(strategy_path: Path, tmp_root: Path, span: float, d: float,
              w: float, task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, span, d, w, run_dir)
    log_path = run_dir / "bt.log"
    day_tokens = [_day_to_token(ROUND_NUM, x) for x in DAYS]
    cmd = ["bash", "backtest/run_bt.sh", str(tmp_strategy),
           *day_tokens, "--merge-pnl", "--out", str(log_path)]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True,
                          env={**os.environ, "PYTHON_BIN": sys.executable})
    if proc.returncode != 0:
        raise RuntimeError(
            f"Backtest failed SPAN={span} D={d} W={w}\nstderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, TARGET_PRODUCT)
    return final_pnl, _compute_sharpe(series)


def _plot_panels(spans: list[float], d_vals: list[float], w_vals: list[float],
                 cube: np.ndarray, title: str, out_path: Path,
                 fmt: str = "{:.0f}") -> None:
    """cube shape: (n_span, n_w, n_d). 每个 SPAN 一个子图."""
    import matplotlib.pyplot as plt

    n_s = len(spans)
    cols = min(n_s, 2)
    rows = (n_s + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(8.5 * cols, 6 * rows))
    if n_s == 1:
        axes = [axes]
    else:
        axes = list(np.array(axes).flatten())

    vmin = float(np.nanmin(cube))
    vmax = float(np.nanmax(cube))

    n_w = len(w_vals)
    n_d = len(d_vals)
    x_step = (d_vals[-1] - d_vals[0]) / max(1, n_d - 1)
    y_step = (w_vals[-1] - w_vals[0]) / max(1, n_w - 1)

    for k, span in enumerate(spans):
        ax = axes[k]
        mat = cube[k]
        im = ax.imshow(
            mat, origin="lower", aspect="auto", cmap="RdYlGn",
            vmin=vmin, vmax=vmax,
            extent=[d_vals[0], d_vals[-1], w_vals[0], w_vals[-1]],
        )
        ax.set_xlabel("D  (price scale)")
        ax.set_ylabel("W  (rebalance step, lots)")
        ax.set_title(f"SPAN={span:g}")
        plt.colorbar(im, ax=ax)

        if n_w * n_d <= 200:
            for i in range(n_w):
                for j in range(n_d):
                    v = mat[i, j]
                    ax.text(
                        d_vals[0] + j * x_step,
                        w_vals[0] + i * y_step,
                        fmt.format(v), ha="center", va="center",
                        fontsize=7, color="black",
                    )

        bi = np.unravel_index(np.nanargmax(mat), mat.shape)
        ax.plot(d_vals[bi[1]], w_vals[bi[0]], "k*", markersize=14,
                label=f"best={mat[bi]:.2f} @ D={d_vals[bi[1]]}, W={w_vals[bi[0]]}")
        ax.legend(loc="upper left")

    for k in range(n_s, len(axes)):
        axes[k].axis("off")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def run_search(strategy_path: Path, spans: list[float], d_vals: list[float],
               w_vals: list[float], outdir: Path, jobs: int) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    n_s = len(spans)
    n_w = len(w_vals)
    n_d = len(d_vals)
    pnl_cube = np.full((n_s, n_w, n_d), np.nan, dtype=float)
    sharpe_cube = np.full((n_s, n_w, n_d), np.nan, dtype=float)
    rows: list[dict] = []

    tasks = [
        (s_idx, w_idx, d_idx, span, w, d)
        for s_idx, span in enumerate(spans)
        for w_idx, w in enumerate(w_vals)
        for d_idx, d in enumerate(d_vals)
    ]
    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="hydrogel_hybrid_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(s_idx, w_idx, d_idx, span, w, d, pnl, sharpe):
            nonlocal done
            pnl_cube[s_idx, w_idx, d_idx] = pnl
            sharpe_cube[s_idx, w_idx, d_idx] = sharpe
            rows.append({"SPAN": span, "D": d, "W": w,
                         "final_pnl": pnl, "sharpe": sharpe})
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{done:>3}/{total}] SPAN={span:>6.0f} D={d:>5.0f} W={w:>3.0f}"
                f"  PnL={pnl:>10.1f}  Sh={sharpe:>6.3f}  ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for s_idx, w_idx, d_idx, span, w, d in tasks:
                pnl, sh = _evaluate(strategy_path, tmp_root, span, d, w,
                                    f"r_{s_idx}_{w_idx}_{d_idx}")
                _consume(s_idx, w_idx, d_idx, span, w, d, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for s_idx, w_idx, d_idx, span, w, d in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root,
                                    span, d, w, f"r_{s_idx}_{w_idx}_{d_idx}")
                    fut_map[fut] = (s_idx, w_idx, d_idx, span, w, d)
                for fut in concurrent.futures.as_completed(fut_map):
                    s_idx, w_idx, d_idx, span, w, d = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(s_idx, w_idx, d_idx, span, w, d, pnl, sh)
    print()

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        wcsv = csv.DictWriter(f, fieldnames=["SPAN", "D", "W", "final_pnl", "sharpe"])
        wcsv.writeheader()
        wcsv.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_panels(spans, d_vals, w_vals, pnl_cube,
                 f"HYDROGEL_PACK final PnL | round={ROUND_NUM}, days={DAYS}",
                 pnl_png, fmt="{:.0f}")
    _plot_panels(spans, d_vals, w_vals, sharpe_cube,
                 f"HYDROGEL_PACK Sharpe | round={ROUND_NUM}, days={DAYS}",
                 sharpe_png, fmt="{:.2f}")

    best_pnl_idx = np.unravel_index(np.nanargmax(pnl_cube), pnl_cube.shape)
    best_sh_idx = np.unravel_index(np.nanargmax(sharpe_cube), sharpe_cube.shape)
    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": ROUND_NUM, "days": DAYS,
        "grid_shape": {"SPAN": n_s, "D": n_d, "W": n_w},
        "best_pnl": {
            "SPAN": spans[best_pnl_idx[0]],
            "W": w_vals[best_pnl_idx[1]],
            "D": d_vals[best_pnl_idx[2]],
            "final_pnl": float(pnl_cube[best_pnl_idx]),
            "sharpe": float(sharpe_cube[best_pnl_idx]),
        },
        "best_sharpe": {
            "SPAN": spans[best_sh_idx[0]],
            "W": w_vals[best_sh_idx[1]],
            "D": d_vals[best_sh_idx[2]],
            "final_pnl": float(pnl_cube[best_sh_idx]),
            "sharpe": float(sharpe_cube[best_sh_idx]),
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

    parser.add_argument("--spans", default="200,1000,3000,10000",
                        help="逗号分隔 SPAN 列表")
    parser.add_argument("--d-start", type=float, default=60.0)
    parser.add_argument("--d-stop", type=float, default=160.0)
    parser.add_argument("--d-step", type=float, default=20.0)
    parser.add_argument("--w-start", type=float, default=20.0)
    parser.add_argument("--w-stop", type=float, default=44.0)
    parser.add_argument("--w-step", type=float, default=6.0)

    args = parser.parse_args()
    spans = _parse_value_list(args.spans)
    d_vals = _frange(args.d_start, args.d_stop, args.d_step)
    w_vals = _frange(args.w_start, args.w_stop, args.w_step)

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(strategy_path)

    print(f"grid: SPAN={spans} × D={d_vals} × W={w_vals} "
          f"= {len(spans)*len(d_vals)*len(w_vals)} pts")

    summary = run_search(
        strategy_path=strategy_path,
        spans=spans, d_vals=d_vals, w_vals=w_vals,
        outdir=Path(args.outdir).resolve(),
        jobs=max(1, args.jobs),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
