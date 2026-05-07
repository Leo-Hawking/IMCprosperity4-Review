"""Round 4 follow_2state grid search (3 days) with plateau scoring.

Searches PnL on round 4 day 0/1/2 using backtest/run_bt.sh and looks for
"plateau" regions by taking the minimum PnL among each point's 1-step
neighbors in the grid (inclusive).

Outputs:
  analysis_outputs/round4_follow2state_psearch/grid.csv
  analysis_outputs/round4_follow2state_psearch/summary.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY = ROOT / "round4trade" / "follow_2state.py"
DEFAULT_OUTDIR = ROOT / "analysis_outputs" / "round4_follow2state_psearch"
ROUND_NUM = 4
DAYS = [0, 1, 2]

# Defaults mirror follow_2state.py
DEFAULT_ENTRY = {
    "HYDROGEL_PACK": 20.0,
    "VEV_4000": 18.0,
    "VELVETFRUIT_EXTRACT": 7.0,
    "VEV_4500": 1.0,
}


@dataclass(frozen=True)
class GridPoint:
    weak_frac: float
    strong_mult_near: float
    strong_mult_taker: float
    entry_scale: float
    follow_max_frac: float


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


def _patch_assign_line(content: str, pattern: str, replacement: str) -> str:
    new_content, n = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if n == 0:
        raise ValueError(f"Pattern not found: {pattern}")
    return new_content


def _build_temp_strategy(
    strategy_path: Path,
    point: GridPoint,
    run_dir: Path,
) -> Path:
    content = strategy_path.read_text(encoding="utf-8")

    # WEAK_FRAC and FOLLOW_MAX_FRAC are uniform dict comprehensions.
    content = _patch_assign_line(
        content,
        r"^WEAK_FRAC\s*:.*$",
        f"WEAK_FRAC: Dict[str, float] = {{p: {point.weak_frac:g} for p in PRODUCTS}}",
    )
    content = _patch_assign_line(
        content,
        r"^FOLLOW_MAX_FRAC\s*:.*$",
        f"FOLLOW_MAX_FRAC: Dict[str, float] = {{p: {point.follow_max_frac:g} for p in PRODUCTS}}",
    )

    # ENTRY_THRESHOLD scaling (only keys present in DEFAULT_ENTRY).
    if abs(point.entry_scale - 1.0) > 1e-9:
        for key, base in DEFAULT_ENTRY.items():
            content = _patch_dict_value(
                content, "ENTRY_THRESHOLD", key, base * point.entry_scale
            )

    # STRONG_THRESHOLD multipliers.
    content = _patch_assign_line(
        content,
        r"STRONG_THRESHOLD\[_p\]\s*=\s*ACTIVE_THRESHOLD\[_p\]",
        f"STRONG_THRESHOLD[_p] = ACTIVE_THRESHOLD[_p] * {point.strong_mult_near:g}",
    )
    content = _patch_assign_line(
        content,
        r"STRONG_THRESHOLD\[_p\]\s*=\s*TAKER_ONLY_THRESHOLD\[_p\]\s*\*\s*[0-9.]+",
        f"STRONG_THRESHOLD[_p] = TAKER_ONLY_THRESHOLD[_p] * {point.strong_mult_taker:g}",
    )

    tmp = run_dir / "strategy_tmp.py"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _parse_total_pnl(log_path: Path) -> float:
    header_idx = {"product": None, "pnl": None}
    last_by_product: dict[str, float] = {}
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
            product = parts[header_idx["product"]]
            try:
                pnl = float(parts[header_idx["pnl"]])
            except ValueError:
                continue
            last_by_product[product] = pnl
    if not last_by_product:
        raise RuntimeError(f"No PnL rows in {log_path}")
    return float(sum(last_by_product.values()))


def _evaluate(
    strategy_path: Path,
    tmp_root: Path,
    point: GridPoint,
    task_id: str,
) -> float:
    run_dir = tmp_root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_strategy = _build_temp_strategy(strategy_path, point, run_dir)
    log_path = run_dir / "bt.log"
    day_tokens = [_day_to_token(ROUND_NUM, d) for d in DAYS]
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
            "Backtest failed "
            f"weak={point.weak_frac} strong_near={point.strong_mult_near} "
            f"strong_taker={point.strong_mult_taker} entry_scale={point.entry_scale} "
            f"follow_max={point.follow_max_frac}\n"
            f"stderr={proc.stderr[-1500:]}"
        )
    return _parse_total_pnl(log_path)


def _plateau_scores(grid: list[GridPoint], pnl: list[float]) -> list[float]:
    # Plateau score = min PnL among 1-step neighbors in index space (incl. self).
    dims = {
        "weak_frac": sorted({p.weak_frac for p in grid}),
        "strong_mult_near": sorted({p.strong_mult_near for p in grid}),
        "strong_mult_taker": sorted({p.strong_mult_taker for p in grid}),
        "entry_scale": sorted({p.entry_scale for p in grid}),
        "follow_max_frac": sorted({p.follow_max_frac for p in grid}),
    }
    index = {
        (p.weak_frac, p.strong_mult_near, p.strong_mult_taker, p.entry_scale, p.follow_max_frac): i
        for i, p in enumerate(grid)
    }

    scores: list[float] = []
    for i, p in enumerate(grid):
        iw = dims["weak_frac"].index(p.weak_frac)
        isn = dims["strong_mult_near"].index(p.strong_mult_near)
        ist = dims["strong_mult_taker"].index(p.strong_mult_taker)
        ie = dims["entry_scale"].index(p.entry_scale)
        ifm = dims["follow_max_frac"].index(p.follow_max_frac)

        candidates: list[float] = []
        for dw in (-1, 0, 1):
            for dn in (-1, 0, 1):
                for dt in (-1, 0, 1):
                    for de in (-1, 0, 1):
                        for df in (-1, 0, 1):
                            if abs(dw) + abs(dn) + abs(dt) + abs(de) + abs(df) > 1:
                                continue
                            jw = iw + dw
                            jn = isn + dn
                            jt = ist + dt
                            je = ie + de
                            jf = ifm + df
                            if jw < 0 or jn < 0 or jt < 0 or je < 0 or jf < 0:
                                continue
                            if jw >= len(dims["weak_frac"]):
                                continue
                            if jn >= len(dims["strong_mult_near"]):
                                continue
                            if jt >= len(dims["strong_mult_taker"]):
                                continue
                            if je >= len(dims["entry_scale"]):
                                continue
                            if jf >= len(dims["follow_max_frac"]):
                                continue
                            key = (
                                dims["weak_frac"][jw],
                                dims["strong_mult_near"][jn],
                                dims["strong_mult_taker"][jt],
                                dims["entry_scale"][je],
                                dims["follow_max_frac"][jf],
                            )
                            idx = index.get(key)
                            if idx is not None:
                                candidates.append(pnl[idx])
        scores.append(min(candidates) if candidates else pnl[i])
    return scores


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--weak-frac", nargs="+", type=float, default=[0.5, 0.6, 0.7, 0.8])
    parser.add_argument("--strong-near", nargs="+", type=float, default=[0.9, 1.0, 1.1, 1.2, 1.3])
    parser.add_argument("--strong-taker", nargs="+", type=float, default=[1.3, 1.5, 1.7, 2.0])
    parser.add_argument("--entry-scale", nargs="+", type=float, default=[0.7, 0.85, 1.0])
    parser.add_argument("--follow-max", nargs="+", type=float, default=[0.3, 0.4, 0.5, 0.6])
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    grid: list[GridPoint] = [
        GridPoint(w, sn, st, es, fm)
        for w in args.weak_frac
        for sn in args.strong_near
        for st in args.strong_taker
        for es in args.entry_scale
        for fm in args.follow_max
    ]

    results: list[float] = [0.0] * len(grid)

    with tempfile.TemporaryDirectory() as tmp_root:
        tmp_root_path = Path(tmp_root)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futures = {}
            for i, point in enumerate(grid):
                task_id = f"case_{i:05d}"
                fut = ex.submit(_evaluate, args.strategy, tmp_root_path, point, task_id)
                futures[fut] = i

            done = 0
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()
                done += 1
                if done % 25 == 0 or done == len(grid):
                    print(f"Progress: {done}/{len(grid)}")

    plateau = _plateau_scores(grid, results)

    grid_path = outdir / "grid.csv"
    with grid_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "weak_frac",
                "strong_mult_near",
                "strong_mult_taker",
                "entry_scale",
                "follow_max_frac",
                "pnl",
                "plateau_score",
            ]
        )
        for point, pnl, score in zip(grid, results, plateau, strict=True):
            writer.writerow(
                [
                    f"{point.weak_frac:g}",
                    f"{point.strong_mult_near:g}",
                    f"{point.strong_mult_taker:g}",
                    f"{point.entry_scale:g}",
                    f"{point.follow_max_frac:g}",
                    f"{pnl:.4f}",
                    f"{score:.4f}",
                ]
            )

    peak_idx = max(range(len(results)), key=lambda i: results[i])
    plateau_idx = max(range(len(plateau)), key=lambda i: plateau[i])

    summary = {
        "peak": {
            **grid[peak_idx].__dict__,
            "pnl": results[peak_idx],
        },
        "plateau": {
            **grid[plateau_idx].__dict__,
            "pnl": results[plateau_idx],
            "plateau_score": plateau[plateau_idx],
        },
        "count": len(grid),
        "outdir": str(outdir),
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print(f"Wrote {grid_path}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
