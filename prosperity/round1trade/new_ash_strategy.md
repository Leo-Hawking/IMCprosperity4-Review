# ASH_COATED_OSMIUM 交易策略

## 参数

| 参数 | 值 | 含义 |
|---|---|---|
| `MU` | 10000 | 长期均值 |
| `Q_MAX` | 80 | 仓位上限 |
| `K` | 8.5 | q\*(p) 饱和点 = 吃单极端阈值 |
| `K_QUOTE` | 9 | 挂单极端阈值（需 > K） |

inner_fair 估计器参数沿用原实现：`VOL_THRESHOLD`, `MAX_STALE_MS`, `HALF_SPREAD_CONST`, `INNER_PRIOR_OFFSET`, `INNER_CONFLICT_TOL`, `INNER_OFFSET_MIN/MAX`。

## 变量

**inner_fair**：沿用原计算——outer_fair 由大单观察构造，inner_fair = outer_fair + 内层 offset（offset clip 在 [−2, 1]，按 0.5 量化）。

## 极端状态（吃单与挂单分离）

定义 `dev = inner_fair − MU`。

- **吃单极端**：|dev| ≥ K（等价于 q\*(inner_fair) 饱和到 ±Q_MAX）
- **挂单极端**：|dev| ≥ K_QUOTE

由 K_QUOTE > K，存在中间区间 K ≤ |dev| < K_QUOTE：吃单已切换为激进模式（放宽门槛），挂单仍双边。

## 目标持仓函数（吃单用）

$$q^*(p) = -Q_{\max} \cdot \text{clip}\left(\frac{p - \mu}{K},\ -1,\ 1\right)$$

## 每 tick 执行

### 前置

计算 inner_fair，失败则跳过本 tick。

### 阶段 1：吃单

吃单门槛：
- 吃单非极端（|dev| < K）：ask_threshold = min(MU, inner_fair)，bid_threshold = max(MU, inner_fair)
- 吃单极端（|dev| ≥ K）：ask_threshold = bid_threshold = MU

**吃 ask**（价格从低到高）：
- ask_px ≥ ask_threshold：停止
- 可吃量 = min(市场该档量, q\*(ask_px) − q, Q_MAX − q)
- > 0 则吃入，更新 q

**吃 bid**（价格从高到低）：
- bid_px ≤ bid_threshold：停止
- 可卖量 = min(市场该档量, q − q\*(bid_px), Q_MAX + q)
- > 0 则卖出，更新 q

### 阶段 2：挂单

使用阶段 1 更新后的 q 和 best_bid / best_ask。

**挂单非极端**（|dev| < K_QUOTE）：**双边挂最大量**
- 买单：price = best_bid + 1, size = Q_MAX − q
- 卖单：price = best_ask − 1, size = Q_MAX + q

**挂单极端**（|dev| ≥ K_QUOTE）：**单边挂最大量**
- dev < −K_QUOTE：只挂买单，price = best_bid + 1, size = Q_MAX − q
- dev > K_QUOTE：只挂卖单，price = best_ask − 1, size = Q_MAX + q

**撤销**：
- 买单 price ≥ MU：撤
- 卖单 price ≤ MU：撤
- size ≤ 0：不挂

## 与上一版的区别

- **极端阈值分离**：上一版 K 三合一（q\*(p) 饱和点 + 吃单极端 + 挂单极端）；本版 K 保留前两者，新增 K_QUOTE 作为挂单极端阈值，要求 K_QUOTE > K。
- **新增中间区间 K ≤ |dev| < K_QUOTE**：吃单切换为激进门槛（用 MU），挂单仍双边。该区间在中等偏离时兼顾调仓速度与做市 spread 收益。
- **参数数增加一个**（K_QUOTE）