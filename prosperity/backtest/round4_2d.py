"""round4follow.py 全资产 (HALFLIFE × 主交易阈值) 2D 高原搜索 (round 4)。

每个产品独立搜：patch 仅修改该产品的 HALFLIFE / 阈值 dict 条目，其它产品默认；
从 activity log 抽取该产品的 PnL 序列得 final PnL 与 Sharpe。

阈值定义：
  - PASSIVE_QUOTE / TAKER_NEAR : ENTRY_THRESHOLD; ACTIVE_THRESHOLD 按原比例同步
  - TAKER_ONLY : TAKER_ONLY_THRESHOLD

输出: analysis_outputs/round4_2d/<product>/{heatmap_pnl,heatmap_sharpe,grid.csv,summary.json}
       analysis_outputs/round4_2d/all_summary.json
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
DEFAULT_STRATEGY = ROOT / "round4trade" / "round4follow_search.py"
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "round4_2d"
ROUND_NUM = 4
DAYS = [1, 2, 3]


PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

# round4follow.py 默认值
DEFAULT_ENTRY = {
    "HYDROGEL_PACK": 20.0, "VEV_4000": 18.0,
    "VELVETFRUIT_EXTRACT": 7.0, "VEV_4500": 1.0,
}
DEFAULT_ACTIVE = {
    "HYDROGEL_PACK": 26.0, "VEV_4000": 34.0,
    "VELVETFRUIT_EXTRACT": 20.0, "VEV_4500": 4.0,
}
DEFAULT_TAKER = {
    "VEV_5000": 18.0, "VEV_5100": 12.0, "VEV_5200": 6.0,
    "VEV_5300": 5.5,  "VEV_5400": 4.0,  "VEV_5500": 4.0,
}

PRODUCTS = (
    sorted(PASSIVE_QUOTE) + sorted(TAKER_NEAR) + sorted(TAKER_ONLY)
)

HALFLIFE_GRID = [500, 1500, 4000, 10000, 25000]
THRESHOLD_SCALE_GRID = [0.5, 0.75, 1.0, 1.5, 2.0]


def _day_to_token(round_num, day):
    return f"{round_num}--{abs(day)}" if day < 0 else f"{round_num}-{day}"


def _patch_dict_value(content, dict_name, key, new_value):
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


def _build_temp_strategy(strategy_path, product, halflife, thr_scale, run_dir):
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_dict_value(content, "HALFLIFE", product, halflife)
    if product in TAKER_ONLY:
        content = _patch_dict_value(
            content, "TAKER_ONLY_THRESHOLD", product,
            DEFAULT_TAKER[product] * thr_scale,
        )
    else:
        new_entry = DEFAULT_ENTRY[product] * thr_scale
        ratio = DEFAULT_ACTIVE[product] / DEFAULT_ENTRY[product]
        new_active = new_entry * ratio
        content = _patch_dict_value(content, "ENTRY_THRESHOLD", product, new_entry)
        content = _patch_dict_value(content, "ACTIVE_THRESHOLD", product, new_active)
    tmp = run_dir / "strategy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _parse_pnl_series(log_path, target):
    header_idx = {"product": None, "pnl": None}
    series = []
    final_pnl = None
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


def _compute_sharpe(pnl_series):
    if len(pnl_series) < 3:
        return 0.0
    arr = np.asarray(pnl_series, dtype=float)
    diffs = np.diff(arr)
    mu = float(np.mean(diffs))
    sd = float(np.std(diffs, ddof=1))
    if sd <= 1e-12:
        return 0.0
    return mu / sd * math.sqrt(len(diffs))


def _evaluate(strategy_path, tmp_root, product, halflife, thr_scale, task_id):
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, product, halflife,
                                        thr_scale, run_dir)
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
            f"Backtest failed product={product} hl={halflife} scale={thr_scale}\n"
            f"stderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plateau_min(matrix):
    out = np.zeros_like(matrix, dtype=float)
    a, b = matrix.shape
    for i in range(a):
        for j in range(b):
            i0, i1 = max(0, i - 1), min(a, i + 2)
            j0, j1 = max(0, j - 1), min(b, j + 2)
            out[i, j] = float(np.nanmin(matrix[i0:i1, j0:j1]))
    return out


def _plot_heatmap(halflives, thr_labels, matrix, plateau, product,
                  default_thr, metric_name, out_path, fmt="{:.0f}"):
    import matplotlib.pyplot as plt

    n_t, n_h = matrix.shape
    fig, ax = plt.subplots(figsize=(9, 6))
    im = ax.imshow(matrix, origin="lower", aspect="auto", cmap="RdYlGn",
                   extent=[0, n_h, 0, n_t])
    ax.set_xticks(np.arange(n_h) + 0.5)
    ax.set_xticklabels([str(int(h)) for h in halflives])
    ax.set_yticks(np.arange(n_t) + 0.5)
    ax.set_yticklabels(thr_labels)
    ax.set_xlabel("HALFLIFE (ticks)")
    ax.set_ylabel(f"threshold (default={default_thr:g})")
    ax.set_title(f"{product} — {metric_name} | round={ROUND_NUM}, days={DAYS}")
    plt.colorbar(im, ax=ax)

    for i in range(n_t):
        for j in range(n_h):
            v = matrix[i, j]
            ax.text(j + 0.5, i + 0.5, fmt.format(v),
                    ha="center", va="center", fontsize=8, color="black")

    pi = np.unravel_index(np.nanargmax(matrix), matrix.shape)
    qi = np.unravel_index(np.nanargmax(plateau), plateau.shape)
    ax.plot(pi[1] + 0.5, pi[0] + 0.5, "k*", markersize=18,
            label=f"peak={matrix[pi]:.2f} @ HL={halflives[pi[1]]}, "
                  f"thr={thr_labels[pi[0]]}")
    ax.plot(qi[1] + 0.5, qi[0] + 0.5, marker="D", color="black",
            markersize=12, mfc="none", mew=2,
            label=f"plateau={plateau[qi]:.2f} (raw={matrix[qi]:.2f}) @ "
                  f"HL={halflives[qi[1]]}, thr={thr_labels[qi[0]]}")
    ax.legend(loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def search_product(strategy_path, product, halflives, scales, outdir, jobs):
    outdir.mkdir(parents=True, exist_ok=True)
    default_thr = (DEFAULT_TAKER[product] if product in TAKER_ONLY
                   else DEFAULT_ENTRY[product])
    thr_labels = [f"{s:g}× ({s * default_thr:g})" for s in scales]

    n_t, n_h = len(scales), len(halflives)
    pnl_mat = np.full((n_t, n_h), np.nan, dtype=float)
    sharpe_mat = np.full((n_t, n_h), np.nan, dtype=float)
    rows = []

    tasks = [(i, j, s, h) for i, s in enumerate(scales) for j, h in enumerate(halflives)]
    total, done, t0 = len(tasks), 0, time.time()

    with tempfile.TemporaryDirectory(prefix=f"r4_2d_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i, j, s, h, pnl, sh):
            nonlocal done
            pnl_mat[i, j] = pnl
            sharpe_mat[i, j] = sh
            rows.append({
                "product": product, "halflife": h,
                "threshold_scale": s,
                "threshold_value": s * default_thr,
                "final_pnl": pnl, "sharpe": sh,
            })
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{product:<22}][{done:>2}/{total}] HL={h:>5} scale={s:.2f}"
                f"  PnL={pnl:>10.1f}  Sh={sh:>6.3f}  ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, j, s, h in tasks:
                pnl, sh = _evaluate(strategy_path, tmp_root, product, h, s,
                                    f"r_{i}_{j}")
                _consume(i, j, s, h, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, j, s, h in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root,
                                    product, h, s, f"r_{i}_{j}")
                    fut_map[fut] = (i, j, s, h)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, j, s, h = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(i, j, s, h, pnl, sh)
    print()

    pnl_plateau = _plateau_min(pnl_mat)
    sharpe_plateau = _plateau_min(sharpe_mat)

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["product", "halflife", "threshold_scale",
                           "threshold_value", "final_pnl", "sharpe"]
        )
        w.writeheader()
        w.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(halflives, thr_labels, pnl_mat, pnl_plateau,
                  product, default_thr, "PnL", pnl_png, fmt="{:.0f}")
    _plot_heatmap(halflives, thr_labels, sharpe_mat, sharpe_plateau,
                  product, default_thr, "Sharpe", sharpe_png, fmt="{:.2f}")

    pi = np.unravel_index(np.nanargmax(pnl_mat), pnl_mat.shape)
    qi = np.unravel_index(np.nanargmax(pnl_plateau), pnl_plateau.shape)
    si = np.unravel_index(np.nanargmax(sharpe_mat), sharpe_mat.shape)
    sqi = np.unravel_index(np.nanargmax(sharpe_plateau), sharpe_plateau.shape)

    summary = {
        "product": product, "default_threshold": default_thr,
        "best_pnl_peak": {"halflife": halflives[pi[1]],
                          "threshold_scale": scales[pi[0]],
                          "threshold_value": scales[pi[0]] * default_thr,
                          "final_pnl": float(pnl_mat[pi]),
                          "sharpe": float(sharpe_mat[pi])},
        "best_pnl_plateau": {"halflife": halflives[qi[1]],
                             "threshold_scale": scales[qi[0]],
                             "threshold_value": scales[qi[0]] * default_thr,
                             "plateau_pnl": float(pnl_plateau[qi]),
                             "raw_pnl": float(pnl_mat[qi]),
                             "sharpe": float(sharpe_mat[qi])},
        "best_sharpe_peak": {"halflife": halflives[si[1]],
                             "threshold_scale": scales[si[0]],
                             "threshold_value": scales[si[0]] * default_thr,
                             "final_pnl": float(pnl_mat[si]),
                             "sharpe": float(sharpe_mat[si])},
        "best_sharpe_plateau": {"halflife": halflives[sqi[1]],
                                "threshold_scale": scales[sqi[0]],
                                "threshold_value": scales[sqi[0]] * default_thr,
                                "plateau_sharpe": float(sharpe_plateau[sqi]),
                                "raw_sharpe": float(sharpe_mat[sqi]),
                                "final_pnl": float(pnl_mat[sqi])},
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY))
    parser.add_argument("--outroot", default=str(DEFAULT_OUTROOT))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--products", nargs="+", default=PRODUCTS)
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    outroot = Path(args.outroot).resolve()
    overall = {}
    for product in args.products:
        outdir = outroot / product.lower()
        summary = search_product(strategy_path, product, HALFLIFE_GRID,
                                 THRESHOLD_SCALE_GRID, outdir, max(1, args.jobs))
        overall[product] = summary

    (outroot / "all_summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"{'product':<22} {'PnL_peak':>10} {'@HL':>7} {'@scale':>7}    "
          f"{'PnL_plateau':>12} {'@HL':>7} {'@scale':>7}    "
          f"{'Sharpe_plateau':>14}")
    print("-"*120)
    for p, s in overall.items():
        pp = s['best_pnl_peak']; pl = s['best_pnl_plateau']
        sp = s['best_sharpe_plateau']
        print(f"{p:<22} {pp['final_pnl']:>10.0f} {pp['halflife']:>7} "
              f"{pp['threshold_scale']:>7.2f}    "
              f"{pl['plateau_pnl']:>12.0f} {pl['halflife']:>7} "
              f"{pl['threshold_scale']:>7.2f}    "
              f"{sp['plateau_sharpe']:>14.3f}")


if __name__ == "__main__":
    main()
