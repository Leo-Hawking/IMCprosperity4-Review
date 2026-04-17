# backtest.py 使用说明

## 数据要求

`data/raw/` 下需有 CSV 文件（分号分隔）：
- `prices_round_{r}_day_{d}.csv` — 含 `timestamp`, `product`, `bid_price_1..3`, `ask_price_1..3`, `bid_volume_1..3`, `ask_volume_1..3`, `mid_price`
- `trades_round_{r}_day_{d}.csv` — 含 `timestamp`, `symbol`, `price`, `quantity`

## 策略接口

策略文件需导出 `Trader` 类：

```python
class Trader:
    def run(self, state: TradingState) -> tuple[dict, int, str]:
        # dict: {symbol: [Order(symbol, price, quantity), ...]}
        # int: conversions (通常为 0)
        # str: traderData (JSON, 下一 tick 回传)
        ...
```

## Python API

```python
from backtest import simulate, simulate_multiday, load_trader_from, plot_results

# 加载策略
trader = load_trader_from("策略v2.py")

# 单日回测
result = simulate(trader, product="ASH_COATED_OSMIUM", day=0, round_num=1)
print(result["summary"])
# {"final_pnl", "peak_pnl", "trough_pnl", "max_drawdown",
#  "max_abs_position", "total_buy_qty", "total_sell_qty", "n_ticks"}

# 三日连续回测（状态跨日延续）
result = simulate_multiday(trader, product="ASH_COATED_OSMIUM", days=[-2, -1, 0])
print(result["summary"]["per_day_pnl"])  # 每日 PnL 拆分

# 绘图（需 record_memory=True 才画 inner_fair）
result = simulate(trader, "ASH_COATED_OSMIUM", day=0, record_memory=True)
plot_results(result, show=True, savepath="output.png")
# 4 个子图：PnL / Position / Mid+inner_fair / Fills
```

## 命令行

```bash
# 基本运行，输出 summary JSON
python backtest.py --product ASH_COATED_OSMIUM --day 0

# 指定策略 + 绘图
python backtest.py --strategy 策略v2.py --day -1 --plot

# 保存图片不弹窗
python backtest.py --strategy 策略v2.py --day 0 --plot --save output.png

# 参数说明
#   --strategy  策略文件路径（默认 策略v2.py）
#   --product   标的名称
#   --day       数据日（-2, -1, 0）
#   --round     轮次（默认 1）
#   --plot      启用绘图
#   --save      图片保存路径（不指定则弹窗显示）
```

## 回测机制

每个 tick 按顺序执行：
1. 加载市场订单簿快照
2. 策略生成订单 → 可成交部分立即吃单成交（逐档撮合）
3. 剩余订单变为挂单
4. 该 tick 的 bot 成交记录与挂单匹配（**严格穿越才成交**，同价对方优先）
5. 未成交挂单清空，不保留到下一 tick

PnL = cash + position * mid（mark-to-market）。
