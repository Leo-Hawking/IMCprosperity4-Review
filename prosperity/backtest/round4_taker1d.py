"""round4follow.py 的 TAKER_ONLY_THRESHOLD 1D 搜索（每个产品独立）。

每个 TAKER_ONLY 产品扫一组阈值；每个值上跑 round 4 三天 (4-1, 4-2, 4-3)
--merge-pnl，取该产品的 final PnL 与 Sharpe。

输出:
  analysis_outputs/round4_taker1d/<product>/curve.csv
  analysis_outputs/round4_taker1d/<product>/curve.png    # PnL & Sharpe 双轴
  analysis_outputs/round4_taker1d/all_curves.png         # 6 产品 panel 总览
  analysis_outputs/round4_taker1d/summary.json
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
DEFAULT_STRATEGY = ROOT / "round4trade" / "round4follow.py"
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "round4_taker1d"
ROUND_NUM = 4
DAYS = [1, 2, 3]

# 默认阈值（与 round4follow.py 保持一致，仅用于标注 baseline）
DEFAULT_THR = {
    "VEV_5000": 18.0, "VEV_5100": 12.0, "VEV_5200": 6.0,
    "VEV_5300": 5.5,  "VEV_5400": 4.0,  "VEV_5500": 4.0,
}

# 每产品自定义 1D 网格（覆盖 0.1× ~ 3× 默认值左右）
GRIDS = {
    "VEV_5000": [1, 3, 6, 9, 12, 15, 18, 21, 25, 30, 40, 50],
    "VEV_5100": [1, 2, 4, 6, 8, 10, 12, 15, 18, 22, 30, 40],
    "VEV_5200": [0.5, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 22],
    "VEV_5300": [0.5, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 22],
    "VEV_5400": [0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 12, 16],
    "VEV_5500": [0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 12, 16],
}


def _day_to_token(round_num: int, day: int) -> str:
    return f"{round_num}--{abs(day)}" if day < 0 else f"{round_num}-{day}"


def _patch_dict_value(content: str, dict_name: str, key: str,
                      new_value: float) -> str:
    pattern = re.compile(
        rf"({re.escape(dict_name)}.*?=\s*\{{.*?\"{re.escape(key)}\":\s*)"
        rf"([0-9]+(?:\.[0-9]+)?)",
        re.DOTALL,
    )
    val_str = f"{new_value:g}"
    new_content, n = pattern.subn(rf"\g<1>{val_str}", content, count=1)
    if n == 0:
        raise ValueError(f"Couldn't patch {dict_name}[{key}]")
    return new_content


def _build_temp_strategy(strategy_path: Path, product: str,
                         threshold: float, run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_dict_value(content, "TAKER_ONLY_THRESHOLD", product, threshold)
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


def _evaluate(strategy_path: Path, tmp_root: Path, product: str,
              threshold: float, task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, product, threshold, run_dir)
    log_path = run_dir / "bt.log"
    day_tokens = [_day_to_token(ROUND_NUM, d) for d in DAYS]
    cmd = ["bash", "backtest/run_bt.sh", str(tmp_strategy),
           *day_tokens, "--merge-pnl", "--out", str(log_path)]
    proc = subprocess.run(
        cmd, cwd=str(ROOT), text=True, capture_output=True,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Backtest failed product={product} thr={threshold}\n"
            f"stderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plot_one_curve(thresholds, pnls, sharpes, product, default_thr, out_path):
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(8.5, 5.5))
    ax2 = ax1.twinx()

    l1, = ax1.plot(thresholds, pnls, "o-", color="tab:green",
                   label="PnL (left)", linewidth=2)
    l2, = ax2.plot(thresholds, sharpes, "s--", color="tab:blue",
                   label="Sharpe (right)", linewidth=1.5, alpha=0.7)

    # mark peak PnL & default
    pi = int(np.nanargmax(pnls))
    ax1.scatter([thresholds[pi]], [pnls[pi]], s=160, marker="*",
                color="black", zorder=5,
                label=f"peak PnL={pnls[pi]:.0f} @ thr={thresholds[pi]}")
    ax1.axvline(default_thr, color="gray", linestyle=":", alpha=0.7,
                label=f"default={default_thr:g}")

    ax1.set_xlabel("TAKER_ONLY_THRESHOLD")
    ax1.set_ylabel("final PnL", color="tab:green")
    ax2.set_ylabel("Sharpe", color="tab:blue")
    ax1.set_title(f"{product} | round={ROUND_NUM}, days={DAYS}")
    ax1.grid(True, alpha=0.3)

    lines = [l1, l2] + [c for c in ax1.get_legend_handles_labels()[0]
                        if c not in (l1,)]
    labels = [l.get_label() for l in lines]
    ax1.legend(lines, labels, loc="best", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _plot_all_panels(results: dict, out_path):
    import matplotlib.pyplot as plt

    products = list(results.keys())
    n = len(products)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, 4.5 * rows))
    axes = list(np.array(axes).flatten())

    for k, product in enumerate(products):
        ax = axes[k]
        ax2 = ax.twinx()
        r = results[product]
        thr = r["thresholds"]; pnl = r["pnls"]; sh = r["sharpes"]

        ax.plot(thr, pnl, "o-", color="tab:green", linewidth=2, label="PnL")
        ax2.plot(thr, sh, "s--", color="tab:blue", linewidth=1.2, alpha=0.7,
                 label="Sharpe")
        pi = int(np.nanargmax(pnl))
        ax.scatter([thr[pi]], [pnl[pi]], s=120, marker="*", color="black",
                   zorder=5)
        ax.axvline(DEFAULT_THR[product], color="gray", linestyle=":", alpha=0.7)

        ax.set_title(f"{product} (default={DEFAULT_THR[product]:g}, "
                     f"peak={pnl[pi]:.0f}@{thr[pi]})")
        ax.set_xlabel("threshold")
        ax.set_ylabel("PnL", color="tab:green")
        ax2.set_ylabel("Sharpe", color="tab:blue")
        ax.grid(True, alpha=0.3)

    for k in range(n, len(axes)):
        axes[k].axis("off")

    fig.suptitle(f"TAKER_ONLY_THRESHOLD 1D scan | round={ROUND_NUM}, days={DAYS}",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def search_product(strategy_path: Path, product: str,
                   thresholds: list[float], outdir: Path,
                   jobs: int) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    n = len(thresholds)
    pnls = [float("nan")] * n
    sharpes = [float("nan")] * n
    rows: list[dict] = []
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix=f"r4_t1d_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i, thr, pnl, sh):
            nonlocal done
            pnls[i] = pnl; sharpes[i] = sh
            rows.append({"product": product, "threshold": thr,
                         "final_pnl": pnl, "sharpe": sh})
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (n - done) if done else 0.0
            print(
                f"\r[{product}][{done:>2}/{n}] thr={thr:>5.2f}"
                f"  PnL={pnl:>9.1f}  Sh={sh:>5.2f}  ETA={eta:>5.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, thr in enumerate(thresholds):
                pnl, sh = _evaluate(strategy_path, tmp_root, product, thr,
                                    f"r_{i}")
                _consume(i, thr, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, thr in enumerate(thresholds):
                    fut = ex.submit(_evaluate, strategy_path, tmp_root,
                                    product, thr, f"r_{i}")
                    fut_map[fut] = (i, thr)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, thr = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(i, thr, pnl, sh)
    print()

    csv_path = outdir / "curve.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product", "threshold", "final_pnl", "sharpe"])
        w.writeheader()
        w.writerows(rows)

    png_path = outdir / "curve.png"
    _plot_one_curve(thresholds, pnls, sharpes, product,
                    DEFAULT_THR[product], png_path)

    pi = int(np.nanargmax(pnls))
    si = int(np.nanargmax(sharpes))
    return {
        "product": product,
        "default_threshold": DEFAULT_THR[product],
        "thresholds": thresholds,
        "pnls": pnls,
        "sharpes": sharpes,
        "best_pnl": {
            "threshold": thresholds[pi],
            "final_pnl": pnls[pi],
            "sharpe": sharpes[pi],
        },
        "best_sharpe": {
            "threshold": thresholds[si],
            "final_pnl": pnls[si],
            "sharpe": sharpes[si],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY))
    parser.add_argument("--outroot", default=str(DEFAULT_OUTROOT))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--products", nargs="+", default=list(GRIDS.keys()))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    outroot = Path(args.outroot).resolve()
    outroot.mkdir(parents=True, exist_ok=True)

    overall: dict[str, dict] = {}
    for product in args.products:
        if product not in GRIDS:
            raise ValueError(f"No grid for {product}")
        outdir = outroot / product.lower()
        summary = search_product(strategy_path, product, GRIDS[product],
                                 outdir, max(1, args.jobs))
        overall[product] = summary

    _plot_all_panels(overall, outroot / "all_curves.png")
    (outroot / "summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n--- best PnL per product ---")
    for p, s in overall.items():
        bp = s["best_pnl"]
        print(f"  {p:<10} default={s['default_threshold']:>5.2f} "
              f"-> best thr={bp['threshold']:>5.2f}  "
              f"PnL={bp['final_pnl']:>9.0f}  Sh={bp['sharpe']:.2f}")


if __name__ == "__main__":
    main()
