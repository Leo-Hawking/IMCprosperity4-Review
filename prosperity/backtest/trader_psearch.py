"""trader.py 单产品 (HALFLIFE × ENTRY_THRESHOLD) 高原搜索。

对 HYDROGEL_PACK 和 VELVETFRUIT_EXTRACT 各做一次 2D 搜索，
patch 对应产品在 HALFLIFE / ENTRY_THRESHOLD 字典里的值，跑 round 3 三天，
按产品过滤 profit_and_loss → 拿到 final PnL 与 Sharpe。

为找「高原」: 用 3×3 邻域的最小值作为 plateau 分数，惩罚孤立的尖刺。
两张热力图 (PnL / Sharpe) 同时标:
  ★ peak  — 单点最优
  ◇ plateau — 邻域最差仍最大的点

输出: analysis_outputs/trader_psearch/<product>/{heatmap_pnl,heatmap_sharpe,grid.csv,summary.json}
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
DEFAULT_STRATEGY = ROOT / "round3trade" / "trader.py"
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "trader_psearch"
ROUND_NUM = 3
DAYS = [0, 1, 2]


# ── search grids ─────────────────────────────────────────────────────────────
PRODUCT_GRIDS = {
    "HYDROGEL_PACK": {
        "halflife": [100, 200, 300, 500, 800, 1200],
        "threshold": [10, 15, 20, 25, 30, 40],
    },
    "VELVETFRUIT_EXTRACT": {
        "halflife": [50, 100, 200, 400, 800, 1500],
        "threshold": [3, 6, 9, 12, 15, 20],
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _day_to_token(round_num: int, day: int) -> str:
    return f"{round_num}--{abs(day)}" if day < 0 else f"{round_num}-{day}"


def _patch_dict_value(content: str, dict_name: str, key: str,
                      new_value: float) -> str:
    """Patch `<dict_name>...= {... "<key>": <num> ...}` 中的数值（第一处）。

    用 DOTALL 让 .*? 跨行；count=1 限定只匹配一次（即第一个该 dict）。
    """
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
                         halflife: float, threshold: float,
                         run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_dict_value(content, "HALFLIFE", product, halflife)
    content = _patch_dict_value(content, "ENTRY_THRESHOLD", product, threshold)
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
              halflife: float, threshold: float,
              task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(
        strategy_path, product, halflife, threshold, run_dir
    )
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
            f"Backtest failed product={product} halflife={halflife} "
            f"threshold={threshold}\nstderr={proc.stderr[-1500:]}"
        )
    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plateau_min(matrix: np.ndarray) -> np.ndarray:
    """3×3 邻域最小值。边角自动收缩窗口。"""
    out = np.zeros_like(matrix, dtype=float)
    a, b = matrix.shape
    for i in range(a):
        for j in range(b):
            i0, i1 = max(0, i - 1), min(a, i + 2)
            j0, j1 = max(0, j - 1), min(b, j + 2)
            out[i, j] = float(np.nanmin(matrix[i0:i1, j0:j1]))
    return out


def _plot_heatmap(halflives: list[float], thresholds: list[float],
                  matrix: np.ndarray, plateau: np.ndarray,
                  title: str, out_path: Path,
                  fmt: str = "{:.0f}") -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6.5))
    im = ax.imshow(
        matrix, origin="lower", aspect="auto", cmap="RdYlGn",
        extent=[halflives[0], halflives[-1], thresholds[0], thresholds[-1]],
    )
    ax.set_xlabel("HALFLIFE  (ticks)")
    ax.set_ylabel("ENTRY_THRESHOLD")
    ax.set_title(title)
    plt.colorbar(im, ax=ax)

    n_t, n_h = matrix.shape
    x_step = (halflives[-1] - halflives[0]) / max(1, n_h - 1)
    y_step = (thresholds[-1] - thresholds[0]) / max(1, n_t - 1)
    if n_t * n_h <= 200:
        for i in range(n_t):
            for j in range(n_h):
                v = matrix[i, j]
                ax.text(
                    halflives[0] + j * x_step,
                    thresholds[0] + i * y_step,
                    fmt.format(v), ha="center", va="center",
                    fontsize=7, color="black",
                )

    pi = np.unravel_index(np.nanargmax(matrix), matrix.shape)
    qi = np.unravel_index(np.nanargmax(plateau), plateau.shape)
    ax.plot(halflives[pi[1]], thresholds[pi[0]], "k*", markersize=18,
            label=f"peak={matrix[pi]:.2f} @ HL={halflives[pi[1]]}, "
                  f"THR={thresholds[pi[0]]}")
    ax.plot(halflives[qi[1]], thresholds[qi[0]], marker="D",
            color="black", markersize=12, mfc="none", mew=2,
            label=f"plateau={plateau[qi]:.2f} (raw={matrix[qi]:.2f}) @ "
                  f"HL={halflives[qi[1]]}, THR={thresholds[qi[0]]}")
    ax.legend(loc="upper left", fontsize=9)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def search_product(strategy_path: Path, product: str,
                   halflives: list[float], thresholds: list[float],
                   outdir: Path, jobs: int) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    n_t = len(thresholds)
    n_h = len(halflives)
    pnl_mat = np.full((n_t, n_h), np.nan, dtype=float)
    sharpe_mat = np.full((n_t, n_h), np.nan, dtype=float)
    rows: list[dict] = []

    tasks = [
        (i, j, t, h)
        for i, t in enumerate(thresholds)
        for j, h in enumerate(halflives)
    ]
    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix=f"trader_psearch_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(i, j, thr, hl, pnl, sh):
            nonlocal done
            pnl_mat[i, j] = pnl
            sharpe_mat[i, j] = sh
            rows.append({
                "product": product, "halflife": hl, "threshold": thr,
                "final_pnl": pnl, "sharpe": sh,
            })
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{product}][{done:>3}/{total}] HL={hl:>5.0f} THR={thr:>4.1f}"
                f"  PnL={pnl:>10.1f}  Sh={sh:>6.3f}  ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for i, j, thr, hl in tasks:
                pnl, sh = _evaluate(strategy_path, tmp_root, product,
                                    hl, thr, f"r_{i}_{j}")
                _consume(i, j, thr, hl, pnl, sh)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for i, j, thr, hl in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root,
                                    product, hl, thr, f"r_{i}_{j}")
                    fut_map[fut] = (i, j, thr, hl)
                for fut in concurrent.futures.as_completed(fut_map):
                    i, j, thr, hl = fut_map[fut]
                    pnl, sh = fut.result()
                    _consume(i, j, thr, hl, pnl, sh)
    print()

    pnl_plateau = _plateau_min(pnl_mat)
    sharpe_plateau = _plateau_min(sharpe_mat)

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["product", "halflife", "threshold", "final_pnl", "sharpe"],
        )
        w.writeheader()
        w.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(halflives, thresholds, pnl_mat, pnl_plateau,
                  f"{product} PnL | round={ROUND_NUM}, days={DAYS}",
                  pnl_png, fmt="{:.0f}")
    _plot_heatmap(halflives, thresholds, sharpe_mat, sharpe_plateau,
                  f"{product} Sharpe | round={ROUND_NUM}, days={DAYS}",
                  sharpe_png, fmt="{:.2f}")

    pi = np.unravel_index(np.nanargmax(pnl_mat), pnl_mat.shape)
    qi = np.unravel_index(np.nanargmax(pnl_plateau), pnl_plateau.shape)
    si = np.unravel_index(np.nanargmax(sharpe_mat), sharpe_mat.shape)
    sqi = np.unravel_index(np.nanargmax(sharpe_plateau), sharpe_plateau.shape)

    summary = {
        "product": product,
        "round": ROUND_NUM, "days": DAYS,
        "grid": {"halflife": halflives, "threshold": thresholds},
        "best_pnl_peak": {
            "halflife": halflives[pi[1]], "threshold": thresholds[pi[0]],
            "final_pnl": float(pnl_mat[pi]), "sharpe": float(sharpe_mat[pi]),
        },
        "best_pnl_plateau": {
            "halflife": halflives[qi[1]], "threshold": thresholds[qi[0]],
            "plateau_pnl": float(pnl_plateau[qi]),
            "raw_pnl": float(pnl_mat[qi]), "sharpe": float(sharpe_mat[qi]),
        },
        "best_sharpe_peak": {
            "halflife": halflives[si[1]], "threshold": thresholds[si[0]],
            "final_pnl": float(pnl_mat[si]), "sharpe": float(sharpe_mat[si]),
        },
        "best_sharpe_plateau": {
            "halflife": halflives[sqi[1]], "threshold": thresholds[sqi[0]],
            "plateau_sharpe": float(sharpe_plateau[sqi]),
            "raw_sharpe": float(sharpe_mat[sqi]),
            "final_pnl": float(pnl_mat[sqi]),
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
    parser.add_argument("--outroot", default=str(DEFAULT_OUTROOT))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--products", nargs="+",
                        default=list(PRODUCT_GRIDS.keys()))
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    outroot = Path(args.outroot).resolve()
    overall: dict[str, dict] = {}

    for product in args.products:
        if product not in PRODUCT_GRIDS:
            raise ValueError(f"No grid defined for {product}")
        grid = PRODUCT_GRIDS[product]
        outdir = outroot / product.lower()
        summary = search_product(
            strategy_path=strategy_path,
            product=product,
            halflives=grid["halflife"],
            thresholds=grid["threshold"],
            outdir=outdir,
            jobs=max(1, args.jobs),
        )
        overall[product] = summary

    print(json.dumps(overall, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
