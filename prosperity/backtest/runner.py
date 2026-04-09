"""Jmerle backtester 调用 + 结果解析。"""

import subprocess
import json
from pathlib import Path

PROSPERITY_DIR = Path(__file__).resolve().parent.parent


def run_backtest(trader_path: str | None = None, round_num: int = 0, day: int = -1) -> dict:
    """
    调用 jmerle backtester。

    需要先安装: pip install prosperity3bt
    用法: prosperity3bt <trader.py> --round <round> --day <day>
    """
    if trader_path is None:
        trader_path = str(PROSPERITY_DIR / "trader.py")

    cmd = [
        "prosperity3bt",
        trader_path,
        "--round", str(round_num),
        "--day", str(day),
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROSPERITY_DIR))

    if result.returncode != 0:
        print(f"STDERR: {result.stderr}")
        return {"success": False, "stderr": result.stderr, "stdout": result.stdout}

    print(result.stdout)
    return {"success": True, "stdout": result.stdout}


def run_all_days(trader_path: str | None = None, round_num: int = 0, days: list[int] | None = None):
    """对所有可用天数跑回测。"""
    if days is None:
        days = [-2, -1]

    results = {}
    for day in days:
        print(f"\n{'='*40} Day {day} {'='*40}")
        results[day] = run_backtest(trader_path, round_num, day)
    return results
