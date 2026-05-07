"""round4follow.py 的 ACTIVE_CROSS_DISTANCE 1D 搜索（每个产品独立，0.5 步进）。

每个产品扫一组 ACTIVE_CROSS_DISTANCE 值；其它产品保持默认；跑 round 4 三天
--merge-pnl，从 activity log 抽取该产品的 final PnL 与 Sharpe。

输出:
  analysis_outputs/round4_acrossd1d/<product>/curve.{csv,png}
  analysis_outputs/round4_acrossd1d/all_curves.png
  analysis_outputs/round4_acrossd1d/summary.json
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
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "round4_acrossd1d"
ROUND_NUM = 4
DAYS = [1, 2, 3]

# round4follow.py 默认 ACTIVE_CROSS_DISTANCE
DEFAULT_ACD = {
    "HYDROGEL_PACK": 8.0,
    "VEV_4000": 12.0,
    "VELVETFRUIT_EXTRACT": 2.5,
    "VEV_4500": 5.0,
    "VEV_5000": 4.0,
    "VEV_5100": 2.5,
    "VEV_5200": 1.5,
    "VEV_5300": 1.5,
    "VEV_5400": 0.75,
    "VEV_5500": 0.75,
}

# 每产品 1D 网格（围绕默认值，0.5 步进）
GRIDS = {
    "HYDROGEL_PACK":       [6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10],
    "VEV_4000":            [8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5],
    "VELVETFRUIT_EXTRACT": [1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5],
    "VEV_4500":            [3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7],
    "VEV_5000":            [2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6],
    "VEV_5100":            [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5],
    "VEV_5200":            [0.5, 1, 1.5, 2, 2.5, 3, 3.5],
    "VEV_5300":            [0.5, 1, 1.5, 2, 2.5, 3, 3.5],
    "VEV_5400":            [0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3],
    "VEV_5500":            [0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3],
}


def _day_to_token(round_num, day):
    return f"{round_num}--{abs(day)}" if day < 0 else f"{round_num}-{day}"


def _patch_dict_value(content, dict_name, key, new_value):
    """Patch `<dict_name>[: Dict[...]]? = {... "<key>": <num> ...}` 中的数值。

    用 (?m)^ + 显式 `:\s*Dict` / `\s*=` 锚定到真正的 dict 定义行，
    避免匹配到 docstring / 注释里出现的同名字面量。
    """
    pattern = re.compile(
        rf"(?ms)^{re.escape(dict_name)}\s*(?::[^=\n]*)?=\s*\{{"
        rf"(?P<body>.*?\"{re.escape(key)}\":\s*)"
        rf"(?P<num>[0-9]+(?:\.[0-9]+)?)",
    )
    val_str = f"{new_value:g}"
    def _sub(m):
        return m.group(0)[:m.start("num") - m.start()] + val_str
    new_content, n = pattern.subn(_sub, content, count=1)
    if n == 0:
        raise ValueError(f"Couldn't patch {dict_name}[{key}]")
    return new_content


def _build_temp_strategy(strategy_path, product, acd, run_dir):
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_dict_value(content, "ACTIVE_CROSS_DISTANCE", product, acd)
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


def _evaluate(strategy_path, tmp_root, product, acd, task_id):
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, product, acd, run_dir)
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
            f"Backtest failed product={product} acd={acd}\n"
            f"stderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plot_one(thresholds, pnls, sharpes, product, default, out_path):
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(8.5, 5.5))
    ax2 = ax1.twinx()
    l1, = ax1.plot(thresholds, pnls, "o-", color="tab:green",
                   label="PnL (left)", linewidth=2)
    l2, = ax2.plot(thresholds, sharpes, "s--", color="tab:blue",
                   label="Sharpe (right)", linewidth=1.5, alpha=0.7)
    pi = int(np.nanargmax(pnls))
    ax1.scatter([thresholds[pi]], [pnls[pi]], s=160, marker="*", color="black",
                zorder=5, label=f"peak PnL={pnls[pi]:.0f} @ ACD={thresholds[pi]}")
    ax1.axvline(default, color="gray", linestyle=":", alpha=0.7,
                label=f"default={default:g}")
    ax1.set_xlabel("ACTIVE_CROSS_DISTANCE")
    ax1.set_ylabel("final PnL", color="tab:green")
    ax2.set_ylabel("Sharpe", color="tab:blue")
    ax1.set_title(f"{product} | round={ROUND_NUM}, days={DAYS}")
    ax1.grid(True, alpha=0.3)
    lines = [l1, l2]
    extras_h, extras_l = ax1.get_legend_handles_labels()
    labels = [l.get_label() for l in lines]
    for h, lab in zip(extras_h, extras_l):
        if h not in lines:
            lines.append(h); labels.append(lab)
    ax1.legend(lines, labels, loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _plot_panels(results, out_path):
    import matplotlib.pyplot as plt

    products = list(results.keys())
    n = len(products)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6.5 * cols, 4.5 * rows))
    axes = list(np.array(axes).flatten())

    for k, product in enumerate(products):
        ax = axes[k]; ax2 = ax.twinx()
        r = results[product]
        thr = r["thresholds"]; pnl = r["pnls"]; sh = r["sharpes"]
        ax.plot(thr, pnl, "o-", color="tab:green", linewidth=2)
        ax2.plot(thr, sh, "s--", color="tab:blue", linewidth=1.2, alpha=0.7)
        pi = int(np.nanargmax(pnl))
        ax.scatter([thr[pi]], [pnl[pi]], s=120, marker="*", color="black", zorder=5)
        ax.axvline(DEFAULT_ACD[product], color="gray", linestyle=":", alpha=0.7)
        ax.set_title(f"{product} (default={DEFAULT_ACD[product]:g}, "
                     f"peak={pnl[pi]:.0f}@{thr[pi]:g})")
        ax.set_xlabel("ACTIVE_CROSS_DISTANCE")
        ax.set_ylabel("PnL", color="tab:green")
        ax2.set_ylabel("Sharpe", color="tab:blue")
        ax.grid(True, alpha=0.3)

    for k in range(n, len(axes)):
        axes[k].axis("off")

    fig.suptitle(f"ACTIVE_CROSS_DISTANCE 1D scan | round={ROUND_NUM}, days={DAYS}",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def search_product(strategy_path, product, acds, outdir, jobs):
    outdir.mkdir(parents=True, exist_ok=True)
    n = len(acds)
    pnls = [float("nan")] * n
    sharpes = [float("nan")] * n
    rows = []
    done, t0 = 0, time.time()

    with tempfile.TemporaryDirectory(prefix=f"r4_acd_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i, a, pnl, sh):
            nonlocal done
            pnls[i] = pnl; sharpes[i] = sh
            rows.append({"product": product, "acd": a,
                         "final_pnl": pnl, "sharpe": sh})
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (n - done) if done else 0.0
            print(f"\r[{product:<22}][{done:>2}/{n}] ACD={a:>5.2f}"
                  f"  PnL={pnl:>9.1f}  Sh={sh:>5.2f}  ETA={eta:>5.1f}s",
                  end="", flush=True)

        if jobs <= 1:
            for i, a in enumerate(acds):
                pnl, sh = _evaluate(strategy_path, tmp_root, product, a, f"r_{i}")
                _consume(i, a, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {ex.submit(_evaluate, strategy_path, tmp_root,
                                     product, a, f"r_{i}"): (i, a)
                           for i, a in enumerate(acds)}
                for fut in concurrent.futures.as_completed(fut_map):
                    i, a = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(i, a, pnl, sh)
    print()

    csv_path = outdir / "curve.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product", "acd", "final_pnl", "sharpe"])
        w.writeheader()
        w.writerows(rows)
    _plot_one(acds, pnls, sharpes, product, DEFAULT_ACD[product],
              outdir / "curve.png")

    pi = int(np.nanargmax(pnls))
    si = int(np.nanargmax(sharpes))
    return {
        "product": product,
        "default_acd": DEFAULT_ACD[product],
        "thresholds": acds, "pnls": pnls, "sharpes": sharpes,
        "best_pnl": {"acd": acds[pi], "final_pnl": pnls[pi], "sharpe": sharpes[pi]},
        "best_sharpe": {"acd": acds[si], "final_pnl": pnls[si], "sharpe": sharpes[si]},
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
        outdir = outroot / product.lower()
        summary = search_product(strategy_path, product, GRIDS[product],
                                 outdir, max(1, args.jobs))
        overall[product] = summary

    _plot_panels(overall, outroot / "all_curves.png")
    (outroot / "summary.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n--- best PnL per product ---")
    for p, s in overall.items():
        bp = s["best_pnl"]
        print(f"  {p:<22} default={s['default_acd']:>5.2f} -> "
              f"best ACD={bp['acd']:>5.2f}  "
              f"PnL={bp['final_pnl']:>9.0f}  Sh={bp['sharpe']:.2f}")


if __name__ == "__main__":
    main()
