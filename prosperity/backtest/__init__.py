"""本地回测 wrapper + 复盘 notebook 的专属目录。

结构:
    backtest/run.py      — prosperity3bt CLI 的 wrapper，注入 P4 产品持仓上限
    backtest/runs/       — 默认输出目录（.log + .json）
    backtest/review.ipynb — 回测复盘 notebook (≤8 cell)
"""
