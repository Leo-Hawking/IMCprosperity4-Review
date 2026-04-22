from __future__ import annotations

import argparse
import csv
import concurrent.futures
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY = ROOT / "round1trade" / "final_root.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "root_hypersearch"
TARGET_PRODUCT = "INTARIAN_PEPPER_ROOT"


def _to_int_seq(start: int, stop: int, step: int) -> list[int]:
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")
    return list(range(start, stop + 1, step))


def _day_to_token(round_num: int, day: int) -> str:
    # run_bt/run.py accepts negative days as <round>--<abs(day)>
    if day < 0:
        return f"{round_num}--{abs(day)}"
    return f"{round_num}-{day}"


def _patch_constant(content: str, name: str, value: int) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(name)}\s*=\s*)([^#\n]+)(.*)$")
    replaced = pattern.sub(rf"\g<1>{value}\g<3>", content, count=1)
    if replaced == content:
        raise ValueError(f"Constant not found: {name}")
    return replaced


def _build_temp_strategy(
    strategy_path: Path,
    min_position: int,
    sell_min_delta: int,
    buy_min_delta: int,
    run_dir: Path,
) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, "MIN_POSITION", min_position)
    content = _patch_constant(content, "PHASE2_SELL_MIN_DELTA", sell_min_delta)
    content = _patch_constant(content, "PHASE2_BUY_MIN_DELTA", buy_min_delta)

    tmp_strategy = run_dir / "strategy_tmp.py"
    tmp_strategy.write_text(content, encoding="utf-8")
    return tmp_strategy


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
                try:
                    product_col = header.index("product")
                    pnl_col = header.index("profit_and_loss")
                except ValueError as exc:
                    raise RuntimeError("Invalid activity log header: missing product/profit_and_loss") from exc
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


def _local_min_plateau_3d(pnl_cube: np.ndarray) -> np.ndarray:
    # Plateau score = local minimum in 3x3x3 neighborhood.
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


def _plot_heatmaps(
    sell_deltas: list[int],
    buy_deltas: list[int],
    max_pnl_map: np.ndarray,
    max_plateau_map: np.ndarray,
    best_minpos_for_pnl: np.ndarray,
    best_minpos_for_plateau: np.ndarray,
    out_path: Path,
    title: str,
) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    extent = [sell_deltas[0], sell_deltas[-1], buy_deltas[0], buy_deltas[-1]]

    im0 = axes[0].imshow(
        max_pnl_map,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[0].set_xlabel("PHASE2_SELL_MIN_DELTA")
    axes[0].set_ylabel("PHASE2_BUY_MIN_DELTA")
    axes[0].set_title("Best PnL over MIN_POSITION")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(np.argmax(max_pnl_map), max_pnl_map.shape)
    axes[0].plot(
        sell_deltas[best_idx[1]],
        buy_deltas[best_idx[0]],
        "k*",
        markersize=14,
        label=f"PnL={max_pnl_map[best_idx]:.0f}, MIN_POS={int(best_minpos_for_pnl[best_idx])}",
    )
    axes[0].legend(loc="upper left")

    im1 = axes[1].imshow(
        max_plateau_map,
        origin="lower",
        aspect="auto",
        extent=extent,
        cmap="RdYlGn",
    )
    axes[1].set_xlabel("PHASE2_SELL_MIN_DELTA")
    axes[1].set_ylabel("PHASE2_BUY_MIN_DELTA")
    axes[1].set_title("Best 3D Plateau Score over MIN_POSITION")
    plt.colorbar(im1, ax=axes[1])

    robust_idx = np.unravel_index(np.argmax(max_plateau_map), max_plateau_map.shape)
    axes[1].plot(
        sell_deltas[robust_idx[1]],
        buy_deltas[robust_idx[0]],
        "k*",
        markersize=14,
        label=(
            f"Plateau={max_plateau_map[robust_idx]:.0f}, "
            f"MIN_POS={int(best_minpos_for_plateau[robust_idx])}"
        ),
    )
    axes[1].legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def _evaluate_one_point(
    strategy_path: Path,
    tmp_root: Path,
    round_num: int,
    days: list[int],
    buy_delta: int,
    sell_delta: int,
    min_pos: int,
    task_id: str,
) -> float:
    day_tokens = [_day_to_token(round_num, d) for d in days]

    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    tmp_strategy = _build_temp_strategy(
        strategy_path=strategy_path,
        min_position=min_pos,
        sell_min_delta=sell_delta,
        buy_min_delta=buy_delta,
        run_dir=run_dir,
    )
    log_path = run_dir / "bt.log"

    cmd = [
        "bash",
        "backtest/run_bt.sh",
        str(tmp_strategy),
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
            "Backtest failed for "
            f"MIN_POSITION={min_pos}, SELL_DELTA={sell_delta}, BUY_DELTA={buy_delta}\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout={proc.stdout[-2000:]}\n"
            f"stderr={proc.stderr[-2000:]}"
        )

    return _parse_final_pnl(log_path, TARGET_PRODUCT)


def run_search(
    strategy_path: Path,
    round_num: int,
    days: list[int],
    min_positions: list[int],
    sell_deltas: list[int],
    buy_deltas: list[int],
    outdir: Path,
    jobs: int,
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    pnl_cube = np.zeros((len(buy_deltas), len(sell_deltas), len(min_positions)), dtype=float)
    rows: list[dict] = []

    total = len(min_positions) * len(sell_deltas) * len(buy_deltas)
    done = 0
    t0 = time.time()

    tasks: list[tuple[int, int, int, int, int, int]] = []
    for i, buy_delta in enumerate(buy_deltas):
        for j, sell_delta in enumerate(sell_deltas):
            for k, min_pos in enumerate(min_positions):
                tasks.append((i, j, k, buy_delta, sell_delta, min_pos))

    with tempfile.TemporaryDirectory(prefix="root_hypersearch_") as tmpdir:
        tmp_root = Path(tmpdir)

        if jobs <= 1:
            for i, j, k, buy_delta, sell_delta, min_pos in tasks:
                pnl = _evaluate_one_point(
                    strategy_path=strategy_path,
                    tmp_root=tmp_root,
                    round_num=round_num,
                    days=days,
                    buy_delta=buy_delta,
                    sell_delta=sell_delta,
                    min_pos=min_pos,
                    task_id=f"r_{i}_{j}_{k}",
                )
                pnl_cube[i, j, k] = pnl
                rows.append(
                    {
                        "round": round_num,
                        "days": ",".join(str(x) for x in days),
                        "min_position": min_pos,
                        "phase2_sell_min_delta": sell_delta,
                        "phase2_buy_min_delta": buy_delta,
                        "final_pnl": pnl,
                    }
                )

                done += 1
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done)
                print(
                    (
                        f"\r[{done:>4}/{total}] min_pos={min_pos:>2} "
                        f"sell_d={sell_delta:>2} buy_d={buy_delta:>2} "
                        f"PnL={pnl:>10.1f} ETA={eta:>7.1f}s"
                    ),
                    end="",
                    flush=True,
                )
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                future_to_params: dict[concurrent.futures.Future, tuple[int, int, int, int, int, int]] = {}
                for i, j, k, buy_delta, sell_delta, min_pos in tasks:
                    fut = ex.submit(
                        _evaluate_one_point,
                        strategy_path,
                        tmp_root,
                        round_num,
                        days,
                        buy_delta,
                        sell_delta,
                        min_pos,
                        f"r_{i}_{j}_{k}",
                    )
                    future_to_params[fut] = (i, j, k, buy_delta, sell_delta, min_pos)

                for fut in concurrent.futures.as_completed(future_to_params):
                    i, j, k, buy_delta, sell_delta, min_pos = future_to_params[fut]
                    pnl = fut.result()
                    pnl_cube[i, j, k] = pnl
                    rows.append(
                        {
                            "round": round_num,
                            "days": ",".join(str(x) for x in days),
                            "min_position": min_pos,
                            "phase2_sell_min_delta": sell_delta,
                            "phase2_buy_min_delta": buy_delta,
                            "final_pnl": pnl,
                        }
                    )

                    done += 1
                    elapsed = time.time() - t0
                    eta = elapsed / done * (total - done)
                    print(
                        (
                            f"\r[{done:>4}/{total}] min_pos={min_pos:>2} "
                            f"sell_d={sell_delta:>2} buy_d={buy_delta:>2} "
                            f"PnL={pnl:>10.1f} ETA={eta:>7.1f}s"
                        ),
                        end="",
                        flush=True,
                    )

    print()

    plateau_cube = _local_min_plateau_3d(pnl_cube)

    max_pnl_map = pnl_cube.max(axis=2)
    max_pnl_arg = pnl_cube.argmax(axis=2)
    best_minpos_for_pnl = np.array([min_positions[idx] for idx in max_pnl_arg.flat]).reshape(max_pnl_arg.shape)

    max_plateau_map = plateau_cube.max(axis=2)
    max_plateau_arg = plateau_cube.argmax(axis=2)
    best_minpos_for_plateau = np.array([min_positions[idx] for idx in max_plateau_arg.flat]).reshape(max_plateau_arg.shape)

    csv_path = outdir / "root_3d_grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "round",
                "days",
                "min_position",
                "phase2_sell_min_delta",
                "phase2_buy_min_delta",
                "final_pnl",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    heatmap_path = outdir / "root_3d_heatmap.png"
    _plot_heatmaps(
        sell_deltas=sell_deltas,
        buy_deltas=buy_deltas,
        max_pnl_map=max_pnl_map,
        max_plateau_map=max_plateau_map,
        best_minpos_for_pnl=best_minpos_for_pnl,
        best_minpos_for_plateau=best_minpos_for_plateau,
        out_path=heatmap_path,
        title=f"ROOT 3D Hypersearch | round={round_num}, days={days}",
    )

    best_idx_3d = np.unravel_index(np.argmax(pnl_cube), pnl_cube.shape)
    robust_idx_3d = np.unravel_index(np.argmax(plateau_cube), plateau_cube.shape)

    summary = {
        "strategy": str(strategy_path),
        "target": TARGET_PRODUCT,
        "round": round_num,
        "days": days,
        "grid_shape": {
            "buy_delta": len(buy_deltas),
            "sell_delta": len(sell_deltas),
            "min_position": len(min_positions),
        },
        "best_pnl_point": {
            "phase2_buy_min_delta": buy_deltas[best_idx_3d[0]],
            "phase2_sell_min_delta": sell_deltas[best_idx_3d[1]],
            "min_position": min_positions[best_idx_3d[2]],
            "final_pnl": float(pnl_cube[best_idx_3d]),
        },
        "best_plateau_point": {
            "phase2_buy_min_delta": buy_deltas[robust_idx_3d[0]],
            "phase2_sell_min_delta": sell_deltas[robust_idx_3d[1]],
            "min_position": min_positions[robust_idx_3d[2]],
            "plateau_score": float(plateau_cube[robust_idx_3d]),
            "final_pnl": float(pnl_cube[robust_idx_3d]),
        },
        "artifacts": {
            "grid_csv": str(csv_path),
            "heatmap_png": str(heatmap_path),
        },
    }

    summary_path = outdir / "root_3d_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "INTARIAN_PEPPER_ROOT 三维超参数搜索："
            "PHASE2_SELL_MIN_DELTA / PHASE2_BUY_MIN_DELTA / MIN_POSITION"
        )
    )
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY), help="Strategy .py path")
    parser.add_argument("--round", type=int, default=1, help="Backtest round")
    parser.add_argument("--days", type=int, nargs="+", default=[-2, -1, 0], help="Days to merge")

    parser.add_argument("--min-pos-start", type=int, default=70)
    parser.add_argument("--min-pos-stop", type=int, default=79)
    parser.add_argument("--min-pos-step", type=int, default=1)

    parser.add_argument("--sell-delta-start", type=int, default=0)
    parser.add_argument("--sell-delta-stop", type=int, default=6)
    parser.add_argument("--sell-delta-step", type=int, default=1)

    parser.add_argument("--buy-delta-start", type=int, default=0)
    parser.add_argument("--buy-delta-stop", type=int, default=6)
    parser.add_argument("--buy-delta-step", type=int, default=1)

    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers")
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    outdir = Path(args.outdir).resolve()

    min_positions = _to_int_seq(args.min_pos_start, args.min_pos_stop, args.min_pos_step)
    sell_deltas = _to_int_seq(args.sell_delta_start, args.sell_delta_stop, args.sell_delta_step)
    buy_deltas = _to_int_seq(args.buy_delta_start, args.buy_delta_stop, args.buy_delta_step)

    summary = run_search(
        strategy_path=strategy_path,
        round_num=args.round,
        days=args.days,
        min_positions=min_positions,
        sell_deltas=sell_deltas,
        buy_deltas=buy_deltas,
        outdir=outdir,
        jobs=max(1, args.jobs),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
