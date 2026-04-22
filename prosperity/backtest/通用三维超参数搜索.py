from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
TARGET_PRODUCT = "ASH_COATED_OSMIUM"


def _day_to_token(round_num: int, day: int) -> str:
    if day < 0:
        return f"{round_num}--{abs(day)}"
    return f"{round_num}-{day}"


def _parse_range_spec(spec: str) -> list[float]:
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid range spec: {spec}. Expected start:stop:step")

    start, stop, step = float(parts[0]), float(parts[1]), float(parts[2])
    if step <= 0:
        raise ValueError("step must be > 0")
    if stop < start:
        raise ValueError("stop must be >= start")

    vals: list[float] = []
    x = start
    eps = step * 1e-6
    while x <= stop + eps:
        vals.append(round(x, 10))
        x += step
    return vals


def _fmt_value(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.10g}"


def _patch_constant(content: str, name: str, value: float) -> str:
    val_str = _fmt_value(value)
    lines = content.splitlines(keepends=True)

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(f"{name}"):
            continue
        if "=" not in stripped:
            continue

        indent = line[: len(line) - len(stripped)]
        body = stripped

        if "#" in body:
            before_comment, comment = body.split("#", 1)
            comment = "#" + comment
        else:
            before_comment, comment = body, ""

        left, _ = before_comment.split("=", 1)
        newline = "\n" if line.endswith("\n") else ""
        if comment:
            lines[i] = f"{indent}{left}= {val_str} {comment.strip()}{newline}"
        else:
            lines[i] = f"{indent}{left}= {val_str}{newline}"
        return "".join(lines)

    raise ValueError(f"Constant not found: {name}")


def _build_temp_strategy(
    strategy_path: Path,
    param_a_name: str,
    param_a_val: float,
    param_b_name: str,
    param_b_val: float,
    param_c_name: str,
    param_c_val: float,
    run_dir: Path,
) -> Path:
    content = strategy_path.read_text(encoding="utf-8")
    content = _patch_constant(content, param_a_name, param_a_val)
    content = _patch_constant(content, param_b_name, param_b_val)
    content = _patch_constant(content, param_c_name, param_c_val)

    tmp_strategy = run_dir / "strategy_tmp.py"
    tmp_strategy.write_text(content, encoding="utf-8")
    return tmp_strategy


def _parse_final_pnl(log_path: Path, target_product: str) -> float:
    header = None
    product_col = None
    pnl_col = None
    last_pnl = None

    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if header is None and line.startswith("day;") and "profit_and_loss" in line:
                header = line.split(";")
                product_col = header.index("product")
                pnl_col = header.index("profit_and_loss")
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


def _plateau_score_3d(pnl_cube: np.ndarray) -> np.ndarray:
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


def _plot_projection_heatmap(
    a_vals: list[float],
    b_vals: list[float],
    max_pnl_map: np.ndarray,
    max_plateau_map: np.ndarray,
    best_c_for_pnl: np.ndarray,
    best_c_for_plateau: np.ndarray,
    param_a_name: str,
    param_b_name: str,
    param_c_name: str,
    out_png: Path,
    title: str,
) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    extent = [a_vals[0], a_vals[-1], b_vals[0], b_vals[-1]]

    im0 = axes[0].imshow(max_pnl_map, origin="lower", aspect="auto", extent=extent, cmap="RdYlGn")
    axes[0].set_xlabel(param_a_name)
    axes[0].set_ylabel(param_b_name)
    axes[0].set_title(f"Best PnL over {param_c_name}")
    plt.colorbar(im0, ax=axes[0])

    best_idx = np.unravel_index(np.argmax(max_pnl_map), max_pnl_map.shape)
    axes[0].plot(
        a_vals[best_idx[1]],
        b_vals[best_idx[0]],
        "k*",
        markersize=14,
        label=f"PnL={max_pnl_map[best_idx]:.1f}, {param_c_name}={best_c_for_pnl[best_idx]:.2f}",
    )
    axes[0].legend(loc="upper left")

    im1 = axes[1].imshow(max_plateau_map, origin="lower", aspect="auto", extent=extent, cmap="RdYlGn")
    axes[1].set_xlabel(param_a_name)
    axes[1].set_ylabel(param_b_name)
    axes[1].set_title(f"Best Plateau over {param_c_name}")
    plt.colorbar(im1, ax=axes[1])

    robust_idx = np.unravel_index(np.argmax(max_plateau_map), max_plateau_map.shape)
    axes[1].plot(
        a_vals[robust_idx[1]],
        b_vals[robust_idx[0]],
        "k*",
        markersize=14,
        label=f"Plateau={max_plateau_map[robust_idx]:.1f}, {param_c_name}={best_c_for_plateau[robust_idx]:.2f}",
    )
    axes[1].legend(loc="upper left")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def run_search(
    strategy_path: Path,
    round_num: int,
    days: list[int],
    target_product: str,
    param_a_name: str,
    param_a_vals: list[float],
    param_b_name: str,
    param_b_vals: list[float],
    param_c_name: str,
    param_c_vals: list[float],
    jobs: int,
    outdir: Path,
) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    pnl_cube = np.zeros((len(param_b_vals), len(param_a_vals), len(param_c_vals)), dtype=float)
    rows: list[dict] = []

    tasks: list[tuple[int, int, int, float, float, float]] = []
    for i, b_val in enumerate(param_b_vals):
        for j, a_val in enumerate(param_a_vals):
            for k, c_val in enumerate(param_c_vals):
                tasks.append((i, j, k, a_val, b_val, c_val))

    total = len(tasks)
    done = 0
    t0 = time.time()

    day_tokens = [_day_to_token(round_num, d) for d in days]

    with tempfile.TemporaryDirectory(prefix="generic_3d_hyper_") as tmpdir:
        tmp_root = Path(tmpdir)

        def worker(params: tuple[int, int, int, float, float, float]):
            i, j, k, a_val, b_val, c_val = params
            run_dir = tmp_root / f"p_{i}_{j}_{k}"
            run_dir.mkdir(parents=True, exist_ok=True)
            strategy_tmp = _build_temp_strategy(
                strategy_path,
                param_a_name,
                a_val,
                param_b_name,
                b_val,
                param_c_name,
                c_val,
                run_dir,
            )
            log_path = run_dir / "bt.log"

            cmd = [
                "bash",
                "backtest/run_bt.sh",
                str(strategy_tmp),
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
                    f"Backtest failed for {param_a_name}={a_val}, {param_b_name}={b_val}, {param_c_name}={c_val}\n"
                    f"cmd={' '.join(cmd)}\n"
                    f"stdout={proc.stdout[-2000:]}\n"
                    f"stderr={proc.stderr[-2000:]}"
                )

            pnl = _parse_final_pnl(log_path, target_product)
            return i, j, k, a_val, b_val, c_val, pnl

        if jobs <= 1:
            iterator = map(worker, tasks)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                future_map = {ex.submit(worker, t): t for t in tasks}
                iterator = (f.result() for f in concurrent.futures.as_completed(future_map))

        for i, j, k, a_val, b_val, c_val, pnl in iterator:
            pnl_cube[i, j, k] = pnl
            rows.append(
                {
                    "round": round_num,
                    "days": ",".join(str(d) for d in days),
                    param_a_name: a_val,
                    param_b_name: b_val,
                    param_c_name: c_val,
                    "final_pnl": pnl,
                }
            )

            done += 1
            elapsed = time.time() - t0
            eta = elapsed / done * (total - done)
            print(
                f"\r[{done:>4}/{total}] {param_a_name}={a_val:>7.3f} {param_b_name}={b_val:>7.3f} {param_c_name}={c_val:>7.3f} PnL={pnl:>10.1f} ETA={eta:>7.1f}s",
                end="",
                flush=True,
            )

    print()

    plateau_cube = _plateau_score_3d(pnl_cube)

    max_pnl_map = pnl_cube.max(axis=2)
    argmax_pnl_map = pnl_cube.argmax(axis=2)
    best_c_for_pnl = np.array([param_c_vals[idx] for idx in argmax_pnl_map.flat], dtype=float).reshape(argmax_pnl_map.shape)

    max_plateau_map = plateau_cube.max(axis=2)
    argmax_plateau_map = plateau_cube.argmax(axis=2)
    best_c_for_plateau = np.array([param_c_vals[idx] for idx in argmax_plateau_map.flat], dtype=float).reshape(argmax_plateau_map.shape)

    csv_path = outdir / "generic_3d_grid.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = ["round", "days", param_a_name, param_b_name, param_c_name, "final_pnl"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    heatmap_path = outdir / "generic_3d_projection_heatmap.png"
    _plot_projection_heatmap(
        a_vals=param_a_vals,
        b_vals=param_b_vals,
        max_pnl_map=max_pnl_map,
        max_plateau_map=max_plateau_map,
        best_c_for_pnl=best_c_for_pnl,
        best_c_for_plateau=best_c_for_plateau,
        param_a_name=param_a_name,
        param_b_name=param_b_name,
        param_c_name=param_c_name,
        out_png=heatmap_path,
        title=f"3D Search | round={round_num}, days={days}",
    )

    best_idx = np.unravel_index(np.argmax(pnl_cube), pnl_cube.shape)
    robust_idx = np.unravel_index(np.argmax(plateau_cube), plateau_cube.shape)

    summary = {
        "strategy": str(strategy_path),
        "target": target_product,
        "round": round_num,
        "days": days,
        "params": [param_a_name, param_b_name, param_c_name],
        "grid_shape": {
            param_b_name: len(param_b_vals),
            param_a_name: len(param_a_vals),
            param_c_name: len(param_c_vals),
        },
        "best_pnl_point": {
            param_b_name: param_b_vals[best_idx[0]],
            param_a_name: param_a_vals[best_idx[1]],
            param_c_name: param_c_vals[best_idx[2]],
            "final_pnl": float(pnl_cube[best_idx]),
        },
        "best_plateau_point": {
            param_b_name: param_b_vals[robust_idx[0]],
            param_a_name: param_a_vals[robust_idx[1]],
            param_c_name: param_c_vals[robust_idx[2]],
            "plateau_score": float(plateau_cube[robust_idx]),
            "final_pnl": float(pnl_cube[robust_idx]),
        },
        "artifacts": {
            "grid_csv": str(csv_path),
            "projection_heatmap_png": str(heatmap_path),
        },
    }

    summary_path = outdir / "generic_3d_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic 3D hyperparameter search for strategy constants")
    parser.add_argument("--strategy", required=True, help="Path to strategy .py")
    parser.add_argument("--target", default=TARGET_PRODUCT, help="Target product for final pnl parse")
    parser.add_argument("--round", type=int, default=2)
    parser.add_argument("--days", type=int, nargs="+", default=[-1, 0, 1])

    parser.add_argument("--param-a", required=True, help="First constant name")
    parser.add_argument("--a-range", required=True, help="First constant range start:stop:step")
    parser.add_argument("--param-b", required=True, help="Second constant name")
    parser.add_argument("--b-range", required=True, help="Second constant range start:stop:step")
    parser.add_argument("--param-c", required=True, help="Third constant name")
    parser.add_argument("--c-range", required=True, help="Third constant range start:stop:step")

    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    strategy_path = Path(args.strategy).resolve()
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    outdir = Path(args.outdir).resolve()
    summary = run_search(
        strategy_path=strategy_path,
        round_num=args.round,
        days=args.days,
        target_product=args.target,
        param_a_name=args.param_a,
        param_a_vals=_parse_range_spec(args.a_range),
        param_b_name=args.param_b,
        param_b_vals=_parse_range_spec(args.b_range),
        param_c_name=args.param_c,
        param_c_vals=_parse_range_spec(args.c_range),
        jobs=max(1, args.jobs),
        outdir=outdir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
