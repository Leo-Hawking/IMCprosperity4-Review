"""OU adaptive K search with plateau-first selection.

针对 round3trade/激进_ou.py：
- 固定 HALFLIFE / POSITION_LIMITS / ACTIVE_CROSS_DISTANCE
- 每次仅 patch 单个产品对应的 K 参数（独立搜索）
- 输出 peak 与 plateau（3x3 邻域最小值）最优点

输出目录:
  analysis_outputs/ou_ksearch/<product>/
    - grid.csv
    - heatmap_pnl.png
    - heatmap_sharpe.png
    - summary.json
  analysis_outputs/ou_ksearch/all_summary.json
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
DEFAULT_STRATEGY = ROOT / "round3trade" / "激进_ou.py"
DEFAULT_OUTROOT = ROOT / "analysis_outputs" / "ou_ksearch"
ROUND_NUM = 3
DAYS = [0, 1, 2]

PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

DEFAULT_K_ENTRY = {
    "HYDROGEL_PACK": 1.6,
    "VEV_4000": 1.5,
    "VELVETFRUIT_EXTRACT": 1.0,
    "VEV_4500": 1.0,
}
DEFAULT_K_ACTIVE = {
    "HYDROGEL_PACK": 2.1,
    "VEV_4000": 2.8,
    "VELVETFRUIT_EXTRACT": 2.8,
    "VEV_4500": 4.0,
}
DEFAULT_K_TAKER = {
    "VEV_5000": 1.5,
    "VEV_5100": 1.5,
    "VEV_5200": 1.5,
    "VEV_5300": 1.4,
    "VEV_5400": 1.3,
    "VEV_5500": 1.3,
}

PRODUCTS = sorted(PASSIVE_QUOTE) + sorted(TAKER_NEAR) + sorted(TAKER_ONLY)

ENTRY_SCALE_GRID = [0.75, 0.9, 1.0, 1.1, 1.25]
ACTIVE_SCALE_GRID = [0.75, 0.9, 1.0, 1.1, 1.25, 1.4]
TAKER_SCALE_GRID = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]


def _day_to_token(round_num: int, day: int) -> str:
    return f"{round_num}--{abs(day)}" if day < 0 else f"{round_num}-{day}"


def _patch_dict_value(content: str, dict_name: str, key: str, new_value: float) -> str:
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
                value = float(parts[header_idx["pnl"]])
            except ValueError:
                continue
            series.append(value)
            final_pnl = value

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


def _plateau_min(matrix: np.ndarray) -> np.ndarray:
    out = np.zeros_like(matrix, dtype=float)
    rows, cols = matrix.shape
    for i in range(rows):
        for j in range(cols):
            i0, i1 = max(0, i - 1), min(rows, i + 2)
            j0, j1 = max(0, j - 1), min(cols, j + 2)
            out[i, j] = float(np.nanmin(matrix[i0:i1, j0:j1]))
    return out


def _safe_nanargmax(matrix: np.ndarray) -> tuple[int, int]:
    if np.isnan(matrix).all():
        raise RuntimeError("All values are NaN in matrix; no valid candidate.")
    return np.unravel_index(np.nanargmax(matrix), matrix.shape)


def _build_temp_strategy(strategy_path: Path, product: str, k1: float, k2: float | None, run_dir: Path) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    if product in TAKER_ONLY:
        content = _patch_dict_value(content, "K_TAKER_ONLY", product, k1)
    else:
        if k2 is None:
            raise ValueError("k2 is required for PASSIVE_QUOTE/TAKER_NEAR")
        content = _patch_dict_value(content, "K_ENTRY", product, k1)
        content = _patch_dict_value(content, "K_ACTIVE", product, k2)

    tmp = run_dir / "strategy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _evaluate(strategy_path: Path, tmp_root: Path, product: str, k1: float, k2: float | None, task_id: str) -> tuple[float, float]:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, product, k1, k2, run_dir)

    log_path = run_dir / "bt.log"
    day_tokens = [_day_to_token(ROUND_NUM, d) for d in DAYS]
    cmd = [
        "bash", "backtest/run_bt.sh", str(tmp_strategy),
        *day_tokens, "--merge-pnl", "--out", str(log_path),
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
            f"Backtest failed product={product} k1={k1} k2={k2}\n"
            f"stderr={proc.stderr[-1500:]}"
        )

    final_pnl, series = _parse_pnl_series(log_path, product)
    return final_pnl, _compute_sharpe(series)


def _plot_heatmap(x_labels: list[str], y_labels: list[str], matrix: np.ndarray, plateau: np.ndarray,
                  title: str, x_name: str, y_name: str, out_path: Path, fmt: str) -> None:
    import matplotlib.pyplot as plt

    n_y, n_x = matrix.shape
    fig, ax = plt.subplots(figsize=(9, 6))
    im = ax.imshow(matrix, origin="lower", aspect="auto", cmap="RdYlGn", extent=[0, n_x, 0, n_y])

    ax.set_xticks(np.arange(n_x) + 0.5)
    ax.set_xticklabels(x_labels)
    ax.set_yticks(np.arange(n_y) + 0.5)
    ax.set_yticklabels(y_labels)
    ax.set_xlabel(x_name)
    ax.set_ylabel(y_name)
    ax.set_title(title)
    plt.colorbar(im, ax=ax)

    if n_x * n_y <= 90:
        for i in range(n_y):
            for j in range(n_x):
                ax.text(j + 0.5, i + 0.5, fmt.format(matrix[i, j]),
                        ha="center", va="center", fontsize=8, color="black")

    peak = np.unravel_index(np.nanargmax(matrix), matrix.shape)
    plat = np.unravel_index(np.nanargmax(plateau), plateau.shape)
    ax.plot(peak[1] + 0.5, peak[0] + 0.5, "k*", markersize=18,
            label=f"peak={matrix[peak]:.3f} @ ({x_labels[peak[1]]}, {y_labels[peak[0]]})")
    ax.plot(plat[1] + 0.5, plat[0] + 0.5, marker="D", color="black", markersize=12,
            mfc="none", mew=2,
            label=(
                f"plateau={plateau[plat]:.3f} (raw={matrix[plat]:.3f})"
                f" @ ({x_labels[plat[1]]}, {y_labels[plat[0]]})"
            ))
    ax.legend(loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _search_product_2d(
    strategy_path: Path,
    product: str,
    outdir: Path,
    jobs: int,
    entry_scales: list[float],
    active_scales: list[float],
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    default_entry = DEFAULT_K_ENTRY[product]
    default_active = DEFAULT_K_ACTIVE[product]

    n_y = len(active_scales)
    n_x = len(entry_scales)
    pnl_mat = np.full((n_y, n_x), np.nan, dtype=float)
    sharpe_mat = np.full((n_y, n_x), np.nan, dtype=float)
    rows: list[dict] = []

    tasks: list[tuple[int, int, float, float]] = []
    for iy, ascale in enumerate(active_scales):
        for ix, escale in enumerate(entry_scales):
            k_entry = default_entry * escale
            k_active = default_active * ascale
            # 保障 bang-bang 区间逻辑仍成立。
            if k_active <= k_entry:
                continue
            tasks.append((iy, ix, k_entry, k_active))

    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix=f"ou_ksearch_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(iy: int, ix: int, k_entry: float, k_active: float, pnl: float, sharpe: float) -> None:
            nonlocal done
            pnl_mat[iy, ix] = pnl
            sharpe_mat[iy, ix] = sharpe
            rows.append({
                "product": product,
                "k_entry": k_entry,
                "k_active": k_active,
                "entry_scale": k_entry / default_entry,
                "active_scale": k_active / default_active,
                "final_pnl": pnl,
                "sharpe": sharpe,
            })
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{product:<22}][{done:>2}/{total}] "
                f"Ke={k_entry:.3f} Ka={k_active:.3f} "
                f"PnL={pnl:>10.1f} Sh={sharpe:>6.3f} ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for iy, ix, k_entry, k_active in tasks:
                pnl, sharpe = _evaluate(strategy_path, tmp_root, product, k_entry, k_active, f"r_{iy}_{ix}")
                _consume(iy, ix, k_entry, k_active, pnl, sharpe)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for iy, ix, k_entry, k_active in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root, product, k_entry, k_active, f"r_{iy}_{ix}")
                    fut_map[fut] = (iy, ix, k_entry, k_active)
                for fut in concurrent.futures.as_completed(fut_map):
                    iy, ix, k_entry, k_active = fut_map[fut]
                    pnl, sharpe = fut.result()
                    _consume(iy, ix, k_entry, k_active, pnl, sharpe)
    print()

    pnl_plateau = _plateau_min(pnl_mat)
    sharpe_plateau = _plateau_min(sharpe_mat)
    pnl_plateau[np.isnan(pnl_mat)] = np.nan
    sharpe_plateau[np.isnan(sharpe_mat)] = np.nan

    x_labels = [f"{default_entry * s:.2f}" for s in entry_scales]
    y_labels = [f"{default_active * s:.2f}" for s in active_scales]

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "product", "k_entry", "k_active", "entry_scale", "active_scale", "final_pnl", "sharpe",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(
        x_labels=x_labels,
        y_labels=y_labels,
        matrix=pnl_mat,
        plateau=pnl_plateau,
        title=f"{product} PnL | round={ROUND_NUM}, days={DAYS}",
        x_name="K_ENTRY",
        y_name="K_ACTIVE",
        out_path=pnl_png,
        fmt="{:.0f}",
    )
    _plot_heatmap(
        x_labels=x_labels,
        y_labels=y_labels,
        matrix=sharpe_mat,
        plateau=sharpe_plateau,
        title=f"{product} Sharpe | round={ROUND_NUM}, days={DAYS}",
        x_name="K_ENTRY",
        y_name="K_ACTIVE",
        out_path=sharpe_png,
        fmt="{:.2f}",
    )

    pi = _safe_nanargmax(pnl_mat)
    qi = _safe_nanargmax(pnl_plateau)
    si = _safe_nanargmax(sharpe_mat)
    sqi = _safe_nanargmax(sharpe_plateau)

    summary = {
        "product": product,
        "mode": "entry_active_2d",
        "default": {
            "k_entry": default_entry,
            "k_active": default_active,
        },
        "grid": {
            "entry_scales": entry_scales,
            "active_scales": active_scales,
        },
        "best_pnl_peak": {
            "k_entry": float(default_entry * entry_scales[pi[1]]),
            "k_active": float(default_active * active_scales[pi[0]]),
            "final_pnl": float(pnl_mat[pi]),
            "sharpe": float(sharpe_mat[pi]),
        },
        "best_pnl_plateau": {
            "k_entry": float(default_entry * entry_scales[qi[1]]),
            "k_active": float(default_active * active_scales[qi[0]]),
            "plateau_pnl": float(pnl_plateau[qi]),
            "raw_pnl": float(pnl_mat[qi]),
            "sharpe": float(sharpe_mat[qi]),
        },
        "best_sharpe_peak": {
            "k_entry": float(default_entry * entry_scales[si[1]]),
            "k_active": float(default_active * active_scales[si[0]]),
            "final_pnl": float(pnl_mat[si]),
            "sharpe": float(sharpe_mat[si]),
        },
        "best_sharpe_plateau": {
            "k_entry": float(default_entry * entry_scales[sqi[1]]),
            "k_active": float(default_active * active_scales[sqi[0]]),
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
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def _search_product_1d(
    strategy_path: Path,
    product: str,
    outdir: Path,
    jobs: int,
    scales: list[float],
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    default_k = DEFAULT_K_TAKER[product]

    n_y = 1
    n_x = len(scales)
    pnl_mat = np.full((n_y, n_x), np.nan, dtype=float)
    sharpe_mat = np.full((n_y, n_x), np.nan, dtype=float)
    rows: list[dict] = []
    tasks = [(0, ix, default_k * s, s) for ix, s in enumerate(scales)]

    total = len(tasks)
    done = 0
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix=f"ou_ksearch_{product}_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _consume(ix: int, k_taker: float, scale: float, pnl: float, sharpe: float) -> None:
            nonlocal done
            pnl_mat[0, ix] = pnl
            sharpe_mat[0, ix] = sharpe
            rows.append({
                "product": product,
                "k_taker_only": k_taker,
                "scale": scale,
                "final_pnl": pnl,
                "sharpe": sharpe,
            })
            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"\r[{product:<22}][{done:>2}/{total}] "
                f"K={k_taker:.3f} PnL={pnl:>10.1f} Sh={sharpe:>6.3f} ETA={eta:>6.1f}s",
                end="", flush=True,
            )

        if jobs <= 1:
            for _, ix, k_taker, scale in tasks:
                pnl, sharpe = _evaluate(strategy_path, tmp_root, product, k_taker, None, f"r_{ix}")
                _consume(ix, k_taker, scale, pnl, sharpe)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                fut_map = {}
                for _, ix, k_taker, scale in tasks:
                    fut = ex.submit(_evaluate, strategy_path, tmp_root, product, k_taker, None, f"r_{ix}")
                    fut_map[fut] = (ix, k_taker, scale)
                for fut in concurrent.futures.as_completed(fut_map):
                    ix, k_taker, scale = fut_map[fut]
                    pnl, sharpe = fut.result()
                    _consume(ix, k_taker, scale, pnl, sharpe)
    print()

    pnl_plateau = _plateau_min(pnl_mat)
    sharpe_plateau = _plateau_min(sharpe_mat)
    pnl_plateau[np.isnan(pnl_mat)] = np.nan
    sharpe_plateau[np.isnan(sharpe_mat)] = np.nan

    x_labels = [f"{default_k * s:.2f}" for s in scales]
    y_labels = ["K_TAKER_ONLY"]

    csv_path = outdir / "grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["product", "k_taker_only", "scale", "final_pnl", "sharpe"],
        )
        w.writeheader()
        w.writerows(rows)

    pnl_png = outdir / "heatmap_pnl.png"
    sharpe_png = outdir / "heatmap_sharpe.png"
    _plot_heatmap(
        x_labels=x_labels,
        y_labels=y_labels,
        matrix=pnl_mat,
        plateau=pnl_plateau,
        title=f"{product} PnL | round={ROUND_NUM}, days={DAYS}",
        x_name="K_TAKER_ONLY",
        y_name="(single-axis search)",
        out_path=pnl_png,
        fmt="{:.0f}",
    )
    _plot_heatmap(
        x_labels=x_labels,
        y_labels=y_labels,
        matrix=sharpe_mat,
        plateau=sharpe_plateau,
        title=f"{product} Sharpe | round={ROUND_NUM}, days={DAYS}",
        x_name="K_TAKER_ONLY",
        y_name="(single-axis search)",
        out_path=sharpe_png,
        fmt="{:.2f}",
    )

    pi = _safe_nanargmax(pnl_mat)
    qi = _safe_nanargmax(pnl_plateau)
    si = _safe_nanargmax(sharpe_mat)
    sqi = _safe_nanargmax(sharpe_plateau)

    summary = {
        "product": product,
        "mode": "taker_only_1d",
        "default": {"k_taker_only": default_k},
        "grid": {"scales": scales},
        "best_pnl_peak": {
            "k_taker_only": float(default_k * scales[pi[1]]),
            "final_pnl": float(pnl_mat[pi]),
            "sharpe": float(sharpe_mat[pi]),
        },
        "best_pnl_plateau": {
            "k_taker_only": float(default_k * scales[qi[1]]),
            "plateau_pnl": float(pnl_plateau[qi]),
            "raw_pnl": float(pnl_mat[qi]),
            "sharpe": float(sharpe_mat[qi]),
        },
        "best_sharpe_peak": {
            "k_taker_only": float(default_k * scales[si[1]]),
            "final_pnl": float(pnl_mat[si]),
            "sharpe": float(sharpe_mat[si]),
        },
        "best_sharpe_plateau": {
            "k_taker_only": float(default_k * scales[sqi[1]]),
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
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def search_product(
    strategy_path: Path,
    product: str,
    outroot: Path,
    jobs: int,
    entry_scales: list[float],
    active_scales: list[float],
    taker_scales: list[float],
) -> dict:
    outdir = outroot / product
    if product in TAKER_ONLY:
        return _search_product_1d(strategy_path, product, outdir, jobs, taker_scales)
    return _search_product_2d(strategy_path, product, outdir, jobs, entry_scales, active_scales)


def _parse_float_list(text: str) -> list[float]:
    vals = [float(x.strip()) for x in text.split(",") if x.strip()]
    if not vals:
        raise ValueError("Scale list cannot be empty.")
    return vals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY))
    parser.add_argument("--outroot", default=str(DEFAULT_OUTROOT))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--products", nargs="+", default=PRODUCTS)
    parser.add_argument(
        "--entry-scales",
        type=_parse_float_list,
        default=ENTRY_SCALE_GRID,
        help="Comma-separated scales for K_ENTRY (e.g. 0.6,0.75,0.9,1.0)",
    )
    parser.add_argument(
        "--active-scales",
        type=_parse_float_list,
        default=ACTIVE_SCALE_GRID,
        help="Comma-separated scales for K_ACTIVE (e.g. 0.5,0.65,0.8,1.0)",
    )
    parser.add_argument(
        "--taker-scales",
        type=_parse_float_list,
        default=TAKER_SCALE_GRID,
        help="Comma-separated scales for K_TAKER_ONLY (e.g. 0.4,0.55,0.7,1.0)",
    )
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    outroot = Path(args.outroot).resolve()
    outroot.mkdir(parents=True, exist_ok=True)

    all_summary = []
    t0 = time.time()

    for idx, product in enumerate(args.products, start=1):
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        print(f"\n=== [{idx}/{len(args.products)}] {product} ===")
        summary = search_product(
            strategy_path,
            product,
            outroot,
            args.jobs,
            args.entry_scales,
            args.active_scales,
            args.taker_scales,
        )
        all_summary.append(summary)
        best = summary["best_pnl_plateau"]
        if product in TAKER_ONLY:
            print(
                f"plateau-pnl best: K={best['k_taker_only']:.3f}, "
                f"plateau={best['plateau_pnl']:.1f}, raw={best['raw_pnl']:.1f}"
            )
        else:
            print(
                f"plateau-pnl best: K_ENTRY={best['k_entry']:.3f}, "
                f"K_ACTIVE={best['k_active']:.3f}, "
                f"plateau={best['plateau_pnl']:.1f}, raw={best['raw_pnl']:.1f}"
            )

    all_path = outroot / "all_summary.json"
    all_path.write_text(json.dumps(all_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed = time.time() - t0
    print(f"\nDone. products={len(args.products)}, elapsed={elapsed:.1f}s")
    print(f"Artifacts: {outroot}")


if __name__ == "__main__":
    main()
