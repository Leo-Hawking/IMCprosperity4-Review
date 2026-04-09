# IMC Prosperity 轻量研究与交易框架

## 项目目标

针对 IMC Prosperity 算法交易竞赛，以**最简架构**完成：微观结构研究 → 策略开发 → 回测验证 → 提交复盘的完整闭环。

设计原则：
- 研究深度 > 工程复杂度
- 参数越少越好，能用一段话说清楚为什么赚钱
- 每次迭代 ≤ 30 分钟

## 开发环境

```bash
source ~/baseChain/defi/bin/activate
```

依赖：`polars`, `plotly`, `numpy`, `pandas`（仅 notebook 分析用）。`trader.py` 提交文件禁止使用任何第三方库。

---

## 项目结构

```
prosperity/
│
├── 1_microstructure.ipynb   # 热文件：微观结构研究
├── 2_strategy_dev.ipynb     # 热文件：策略开发 + 向量化快速回测
├── 3_review.ipynb           # 热文件：提交复盘 + PnL 归因
│
├── trader.py                # 提交文件，自包含，< 300 行
│
├── backtest/
│   └── runner.py            # 调用 Jmerle backtester + 结果解析
│
├── utils/
│   ├── dataio.py            # CSV/parquet 读写
│   ├── orderbook.py         # wall_mid、spread 等计算
│   └── viz.py               # 通用绘图函数
│
├── configs/
│   └── default.yaml         # 少量参数（per product）
│
└── data/
    ├── raw/                  # IMC 原始 CSV
    ├── processed/            # parquet
    └── submissions/          # 官网提交结果日志
```

### 热文件 vs 冷文件

| 类型 | 格式 | 修改频率 | 内容 |
|------|------|---------|------|
| 热文件 | `.ipynb` | 每轮多次 | 可视化、探索、debug、复盘 |
| 冷文件 | `.py` | 稳定后很少改 | 工具函数、数据加载、回测 runner |
| 提交文件 | `trader.py` | 每轮迭代 | 策略核心，自包含 |

原则：能在 notebook 里做的不拆文件，能 import 的不复制粘贴。

---

## IMC Trader 接口规范

IMC 要求提交一个 Python 文件，其中必须包含 `class Trader`，实现 `run` 方法。以下是完整的接口约定。

### 入口签名

```python
class Trader:
    def run(self, state: TradingState) -> tuple[dict[str, list[Order]], int, str]:
        """
        每个 timestep 被调用一次。

        参数:
            state: TradingState — 当前市场快照

        返回:
            (orders, conversions, traderData)
            - orders: dict[str, list[Order]]  — 每个产品的订单列表
            - conversions: int                — 转换请求数量（Round 4+ 用）
            - traderData: str                 — 持久化状态，下一个 timestep 原样回传
        """
```

### TradingState 结构

```python
class TradingState:
    traderData: str                        # 上一步 run() 返回的 traderData
    timestamp: int                         # 当前时间戳（0, 100, 200, ...）
    listings: dict[str, Listing]           # 产品信息
    order_depths: dict[str, OrderDepth]    # 每个产品的当前 orderbook
    own_trades: dict[str, list[Trade]]     # 上一步自己的成交
    market_trades: dict[str, list[Trade]]  # 上一步市场上的成交
    position: dict[str, int]              # 当前持仓（product -> quantity）
    observations: Observation              # 外部观测值（Round 4+ 用）
```

### OrderDepth 结构

```python
class OrderDepth:
    buy_orders: dict[int, int]    # price -> volume（正数）
    sell_orders: dict[int, int]   # price -> volume（负数！注意符号）
```

**关键细节：`sell_orders` 的 volume 是负数。** 例如 `{10002: -5}` 表示有人在 10002 挂了 5 手卖单。下单吃这个 ask 时，你的 Order quantity 用正数。

### Order 结构

```python
class Order:
    def __init__(self, symbol: str, price: int, quantity: int):
        """
        quantity > 0: 买单
        quantity < 0: 卖单
        price: 整数，IMC 的价格都是整数
        """
```

### Trade 结构

```python
class Trade:
    symbol: str
    price: int
    quantity: int      # 成交数量（正数）
    buyer: str         # 买方 ID（Round 5 可见，其他轮为空或匿名）
    seller: str        # 卖方 ID
    timestamp: int
```

### 持仓限制（Round 1）

```python
POSITION_LIMITS = {
    "RAINFOREST_RESIN": 50,    # 稳定资产，fair = 10000
    "KELP": 50,                # 漂移资产，fair ≈ wall_mid
}
```

超出限制的订单会被 IMC 自动拒绝，不会部分成交。你必须自己保证下单后的预期持仓不超限。

### 执行顺序（每个 timestep）

```
1. 清除上一步所有订单（订单只存活一个 timestep）
2. Maker bot 挂单（形成 orderbook 的 wall 等深度）
3. 部分 Taker bot 吃单
4. 你的 run(state) 被调用，拿到当前 orderbook snapshot
5. 你的订单被处理：
   - 价格穿过对手盘的订单立即成交（taker）
   - 未成交的订单挂在 book 上（maker）
6. 其他 bot 继续交易（可能吃掉你的挂单）
```

**含义：** 你看到的 orderbook 已经包含了 maker bot 的报价。你可以选择吃单（aggressive）或挂单（passive），两者都在同一步完成，不存在速度竞争。

### traderData 用法

`traderData` 是你在 timestep 之间传递状态的唯一方式。IMC 不保留你的全局变量——每次 `run()` 调用都是无状态的，只有 `traderData` 会被原样回传。

```python
import json

class Trader:
    def run(self, state):
        # 恢复状态
        data = json.loads(state.traderData) if state.traderData else {}

        # ... 策略逻辑 ...

        # 保存状态
        traderData = json.dumps(data)
        return orders, 0, traderData
```

### trader.py 最小模板

```python
import json
from datamodel import Order, TradingState

LIMITS = {"RAINFOREST_RESIN": 50, "KELP": 50}

class Trader:
    def run(self, state: TradingState):
        orders = {}
        for product in state.order_depths:
            orders[product] = self.trade(product, state)
        data = json.dumps({})
        return orders, 0, data

    def trade(self, product, state):
        order_depth = state.order_depths[product]
        position = state.position.get(product, 0)
        limit = LIMITS[product]
        orders = []

        fair = self.get_fair(product, order_depth)

        # 1) 吃掉所有有利单
        for ask, vol in sorted(order_depth.sell_orders.items()):
            if ask < fair and position < limit:
                qty = min(-vol, limit - position)
                orders.append(Order(product, ask, qty))
                position += qty

        for bid, vol in sorted(order_depth.buy_orders.items(), reverse=True):
            if bid > fair and position > -limit:
                qty = min(vol, position + limit)
                orders.append(Order(product, bid, -qty))
                position -= qty

        # 2) 挂 penny 单
        if order_depth.buy_orders and order_depth.sell_orders:
            best_bid = max(order_depth.buy_orders)
            best_ask = min(order_depth.sell_orders)

            my_bid = min(best_bid + 1, round(fair) - 1)
            my_ask = max(best_ask - 1, round(fair) + 1)

            if position < limit:
                orders.append(Order(product, my_bid, limit - position))
            if position > -limit:
                orders.append(Order(product, my_ask, -(limit + position)))

        return orders

    def get_fair(self, product, od):
        if product == "RAINFOREST_RESIN":
            return 10000

        # 漂移资产：wall mid
        bid_wall = max(od.buy_orders, key=lambda p: od.buy_orders[p])
        ask_wall = min(od.sell_orders, key=lambda p: abs(od.sell_orders[p]))
        return (bid_wall + ask_wall) / 2
```

---

## 迭代工作流

```
1_microstructure.ipynb    观察 orderbook、识别 bot 行为、验证 fair 估计
        ↓
2_strategy_dev.ipynb      写策略逻辑 → 搬入 trader.py → 向量化快速回测
        ↓
trader.py                 Jmerle backtester 跑完整回测 → 官网提交
        ↓
3_review.ipynb            复盘 PnL、找偏差原因
        ↓
回到 1_microstructure     带着新问题重新观察
```

每次循环目标 ≤ 30 分钟。如果超时，检查是工具不够自动化还是在调参——如果是后者，停下来回到 notebook 1 看数据。

---

## 回测策略

- **不自建撮合引擎。** 被动成交的 fill probability 取决于后续 bot 行为，无法准确模拟。
- **Jmerle backtester** 用于 taker 策略的快速验证和参数粗筛。
- **官网提交** 用于验证 maker fill 和 bot 交互行为（每轮有提交次数限制，珍惜使用）。
- **Jupyter 向量化回测** 用于早期探索（不需要 tick-by-tick，只需验证信号方向）。
- **永远不要纯粹优化官网分数**，那是 overfitting。