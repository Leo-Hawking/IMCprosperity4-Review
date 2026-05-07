"""round4follow.py 的 ENTRY_THRESHOLD 1D 搜索（PASSIVE_QUOTE + TAKER_NEAR）。

针对 HYDROGEL_PACK, VEV_4000 (PASSIVE_QUOTE) 与 VELVETFRUIT_EXTRACT, VEV_4500
(TAKER_NEAR) 各扫一组 ENTRY_THRESHOLD；ACTIVE_THRESHOLD 按原始 ACTIVE/ENTRY
比例同步缩放，保留原文件的相对结构。

输出: analysis_outputs/round4_entry1d/<product>/curve.{csv,png}
       analysis_outputs/round4_entry1d/all_curves.png
       analysis_outputs/round4_entry1d/summary.json
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
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "round4_entry1d"
ROUND_NUM = 4
DAYS = [1, 2, 3]

# round4follow.py 默认值
DEFAULT_ENTRY = {
    "HYDROGEL_PACK": 20.0, "VEV_4000": 18.0,
    "VELVETFRUIT_EXTRACT": 7.0, "VEV_4500": 1.0,
}
DEFAULT_ACTIVE = {
    "HYDROGEL_PACK": 26.0, "VEV_4000": 34.0,
    "VELVETFRUIT_EXTRACT": 20.0, "VEV_4500": 4.0,
}

# 每产品自定义 1D entry 网格
GRIDS = {
    "HYDROGEL_PACK":       [5, 8, 12, 15, 18, 20, 22, 25, 30, 40, 60],
    "VEV_4000":            [3, 6, 10, 14, 18, 22, 25, 30, 40, 60],
    "VELVETFRUIT_EXTRACT": [1, 2, 4, 6, 7, 8, 10, 12, 15, 20, 30],
    "VEV_4500":            [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 12, 18],
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
                         entry: float, run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    ratio = DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product]
    active = entry * ratio
    content = _patch_dict_value(content, "ENTRY_THRESHOLD", product, entry)
    content = _patch_dict_value(content, "ACTIVE_THRESHOLD", product, active)
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


def _evaluate(strategy_path, tmp_root, product, entry, task_id):
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, product, entry, run_dir)
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
            f"Backtest failed product={product} entry={entry}\n"
            f"stderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plot_one_curve(thresholds, pnls, sharpes, product, default_entry,
                    default_active, out_path):
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(8.5, 5.5))
    ax2 = ax1.twinx()

    l1, = ax1.plot(thresholds, pnls, "o-", color="tab:green",
                   label="PnL (left)", linewidth=2)
    l2, = ax2.plot(thresholds, sharpes, "s--", color="tab:blue",
                   label="Sharpe (right)", linewidth=1.5, alpha=0.7)

    pi = int(np.nanargmax(pnls))
    ax1.scatter([thresholds[pi]], [pnls[pi]], s=160, marker="*",
                color="black", zorder=5,
                label=f"peak PnL={pnls[pi]:.0f} @ ENTRY={thresholds[pi]}")
    ax1.axvline(default_entry, color="gray", linestyle=":", alpha=0.7,
                label=f"default ENTRY={default_entry:g} (ACTIVE={default_active:g})")

    ax1.set_xlabel("ENTRY_THRESHOLD  (ACTIVE 按原比例同步缩放)")
    ax1.set_ylabel("final PnL", color="tab:green")
    ax2.set_ylabel("Sharpe", color="tab:blue")
    ax1.set_title(f"{product} | round={ROUND_NUM}, days={DAYS}")
    ax1.grid(True, alpha=0.3)

    lines = [l1, l2]
    labels = [l.get_label() for l in lines]
    extras_h, extras_l = ax1.get_legend_handles_labels()
    for h, lab in zip(extras_h, extras_l):
        if h not in lines:
            lines.append(h); labels.append(lab)
    ax1.legend(lines, labels, loc="best", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _plot_all_panels(results, out_path):
    import matplotlib.pyplot as plt

    products = list(results.keys())
    n = len(products)
    cols = 2
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(7 * cols, 4.7 * rows))
    axes = list(np.array(axes).flatten())

    for k, product in enumerate(products):
        ax = axes[k]; ax2 = ax.twinx()
        r = results[product]
        thr = r["thresholds"]; pnl = r["pnls"]; sh = r["sharpes"]
        ax.plot(thr, pnl, "o-", color="tab:green", linewidth=2)
        ax2.plot(thr, sh, "s--", color="tab:blue", linewidth=1.2, alpha=0.7)
        pi = int(np.nanargmax(pnl))
        ax.scatter([thr[pi]], [pnl[pi]], s=120, marker="*", color="black",
                   zorder=5)
        ax.axvline(DEFAULT_ENTRY[product], color="gray", linestyle=":",
                   alpha=0.7)
        ax.set_title(f"{product} (default={DEFAULT_ENTRY[product]:g}, "
                     f"peak={pnl[pi]:.0f}@{thr[pi]:g})")
        ax.set_xlabel("ENTRY_THRESHOLD")
        ax.set_ylabel("PnL", color="tab:green")
        ax2.set_ylabel("Sharpe", color="tab:blue")
        ax.grid(True, alpha=0.3)

    for k in range(n, len(axes)):
        axes[k].axis("off")

    fig.suptitle(f"ENTRY_THRESHOLD 1D scan (ACTIVE follows ratio) | "
                 f"round={ROUND_NUM}, days={DAYS}", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def search_product(strategy_path, product, entries, outdir, jobs):
    outdir.mkdir(parents=True, exist_ok=True)
    n = len(entries)
    pnls = [float("nan")] * n
    sharpes = [float("nan")] * n
    rows = []
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix=f"r4_e1d_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i, e, pnl, sh):
            nonlocal done
            pnls[i] = pnl; sharpes[i] = sh
            rows.append({"product": product, "entry": e,
                         "active": e * DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product],
                         "final_pnl": pnl, "sharpe": sh})
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (n - done) if done else 0.0
            print(
                f"\r[{product:<22}][{done:>2}/{n}] entry={e:>5.2f}"
                f"  PnL={pnl:>9.1f}  Sh={sh:>5.2f}  ETA={eta:>5.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, e in enumerate(entries):
                pnl, sh = _evaluate(strategy_path, tmp_root, product, e,
                                    f"r_{i}")
                _consume(i, e, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, e in enumerate(entries):
                    fut = ex.submit(_evaluate, strategy_path, tmp_root,
                                    product, e, f"r_{i}")
                    fut_map[fut] = (i, e)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, e = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(i, e, pnl, sh)
    print()

    csv_path = outdir / "curve.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product", "entry", "active",
                                          "final_pnl", "sharpe"])
        w.writeheader()
        w.writerows(rows)
    png_path = outdir / "curve.png"
    _plot_one_curve(entries, pnls, sharpes, product,
                    DEFAULT_ENTRY[product], DEFAULT_ACTIVE[product], png_path)

    pi = int(np.nanargmax(pnls))
    si = int(np.nanargmax(sharpes))
    return {
        "product": product,
        "default_entry": DEFAULT_ENTRY[product],
        "default_active": DEFAULT_ACTIVE[product],
        "active_over_entry_ratio": DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product],
        "thresholds": entries,
        "pnls": pnls, "sharpes": sharpes,
        "best_pnl": {
            "entry": entries[pi],
            "active": entries[pi] * DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product],
            "final_pnl": pnls[pi], "sharpe": sharpes[pi],
        },
        "best_sharpe": {
            "entry": entries[si],
            "active": entries[si] * DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product],
            "final_pnl": pnls[si], "sharpe": sharpes[si],
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY))
    parser.add_argument("--outroot", default=str(DEFAULT_OUTROOT))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--products", nargs="+", default=list(GRIDS.keys()))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    outroot = Path(args.outroot).resolve()
    outroot.mkdir(parents=True, exist_ok=True)

    overall = {}
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
        print(f"  {p:<22} default ENTRY={s['default_entry']:>5.2f} -> "
              f"best ENTRY={bp['entry']:>5.2f} (ACTIVE={bp['active']:>5.2f})  "
              f"PnL={bp['final_pnl']:>9.0f}  Sh={bp['sharpe']:.2f}")


if __name__ == "__main__":
    main()
