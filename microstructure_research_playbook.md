# IMC Prosperity 微观结构研究手册


---

## 零、核心思维转变

"厚度、弹性、高频因子"是真实市场的分析框架。但 IMC Prosperity 是**模拟市场**，有几个根本不同：

1. **没有延迟竞争**：每个 timestep 你拿到完整 snapshot，所有人同时行动，不存在速度优势
2. **Bot 行为是确定性程序**：不是真人，行为有规律可循，甚至可以精确预测
3. **订单只存活一个 timestep**：没有挂单排队概念，每步重新提交
4. **成交顺序是固定的**：先 maker bot → taker bot → 你 → 其他 bot

所以你需要研究的不是"市场有多厚"，而是：**谁在交易、什么时候交易、什么价格交易、交易多少**。

---

## 一、必做可视化（按优先级排序）

工具：主要使用plotly

### 1. Orderbook 散点时序图（最重要）

Frankfurt Hedgehogs 的 Dashboard 核心就是这个。

**横轴**：timestamp  
**纵轴**：price  
**画什么**：
- 每个 timestamp 的每一档 bid（蓝点）和 ask（红点），点的大小 ∝ 该档数量
- 成交：用不同 marker 区分 buyer-initiated vs seller-initiated
- 你自己的挂单和成交（如果有回放数据）

**为什么重要**：这张图一眼能看出：
- 哪些价位总是有大量挂单（= wall）
- 价格是固定的、慢漂移的、还是跳跃的
- 成交发生在 spread 内部还是 wall 上
- 有没有 bot 在固定时刻/固定价格出现

```python
import plotly.graph_objects as go

def plot_orderbook_scatter(prices_df, trades_df, product, day):
    """
    prices_df 需要列: timestamp, product, side(bid/ask), price, volume
    trades_df 需要列: timestamp, product, price, quantity, buyer, seller
    """
    fig = go.Figure()
    
    p = prices_df[(prices_df.product == product) & (prices_df.day == day)]
    
    # Bid levels - 点大小反映挂单量
    bids = p[p.side == 'bid']
    fig.add_trace(go.Scatter(
        x=bids.timestamp, y=bids.price,
        mode='markers',
        marker=dict(color='steelblue', size=bids.volume / bids.volume.max() * 15,
                    opacity=0.5),
        name='Bids'
    ))
    
    # Ask levels
    asks = p[p.side == 'ask']
    fig.add_trace(go.Scatter(
        x=asks.timestamp, y=asks.price,
        mode='markers',
        marker=dict(color='salmon', size=asks.volume / asks.volume.max() * 15,
                    opacity=0.5),
        name='Asks'
    ))
    
    # Trades
    t = trades_df[(trades_df.product == product) & (trades_df.day == day)]
    fig.add_trace(go.Scatter(
        x=t.timestamp, y=t.price,
        mode='markers',
        marker=dict(color='black', symbol='x', size=8),
        name='Trades'
    ))
    
    fig.update_layout(title=f'{product} Day {day}', height=600)
    return fig
```

### 2. Wall 识别图

在每个 timestamp，找 bid 侧和 ask 侧**挂单量最大的价位**（= wall）。

```python
def find_walls(order_depth):
    """从单个 timestamp 的 order_depth 提取 wall 价位"""
    bid_wall_price = max(order_depth.buy_orders.keys(),
                         key=lambda p: order_depth.buy_orders[p])
    ask_wall_price = min(order_depth.sell_orders.keys(),
                         key=lambda p: abs(order_depth.sell_orders[p]))
    wall_mid = (bid_wall_price + ask_wall_price) / 2
    return bid_wall_price, ask_wall_price, wall_mid
```

**画什么**：wall_mid 的时序线，叠加在 orderbook 散点图上。

**看什么**：
- wall_mid 是否稳定？如果几乎不动 → 稳定资产（如 Resin），fair = 常数
- wall_mid 是否平滑漂移？→ 漂移资产（如 Kelp），fair = wall_mid
- wall 和 raw mid（best_bid + best_ask / 2）差多少？差距大说明 raw mid 被噪声污染严重，不可直接用

### 3. 成交分类图

IMC 的 trades 数据通常包含 buyer 和 seller 字段（或至少能推断方向）。

**按交易者分类统计**：
```python
def trade_profile(trades_df, product):
    """每个交易者的行为画像"""
    t = trades_df[trades_df.product == product]
    
    # 按 buyer/seller 聚合
    buy_stats = t.groupby('buyer').agg(
        count=('quantity', 'count'),
        total_qty=('quantity', 'sum'),
        avg_qty=('quantity', 'mean'),
        avg_price=('price', 'mean'),
        price_std=('price', 'std'),
        first_ts=('timestamp', 'min'),
        last_ts=('timestamp', 'max')
    ).sort_values('total_qty', ascending=False)
    
    return buy_stats
```

**看什么**：
- 谁总是买？谁总是卖？谁双边都做？
- 某个 bot 是不是总在固定数量交易？（Olivia = 总是 15 手）
- 某个 bot 的成交价和 fair 的关系（总在 daily high/low 出现？）
- 成交量是否在一天内有时间分布规律？

### 4. Normalized Orderbook 图（去趋势）

对漂移资产，把所有价格减去 wall_mid，得到**围绕 fair 的相对价格**。

```python
# 每个 timestamp 的价格都减去该时刻的 wall_mid
normalized_price = raw_price - wall_mid_at_t
```

**画什么**：normalized 后的 orderbook 散点图（同图1的结构，但 y 轴变成偏离 fair 的 tick 数）。

**为什么重要**：去趋势后，漂移资产看起来就像稳定资产。你能直接看到：
- spread 通常是几个 tick
- 哪些 tick 距离有大量挂单
- 成交通常发生在偏离 fair 几个 tick 的位置

---

## 二、必做统计检验

### 1. Return Autocorrelation（判断是否可预测）

```python
import numpy as np

def return_autocorrelation(wall_mid_series, max_lag=20):
    """
    对 wall_mid 的 return 序列做自相关分析
    """
    returns = np.diff(wall_mid_series)
    n = len(returns)
    
    autocorrs = []
    for lag in range(1, max_lag + 1):
        c = np.corrcoef(returns[:-lag], returns[lag:])[0, 1]
        autocorrs.append(c)
    
    # 95% 置信区间（白噪声假设下）
    ci = 1.96 / np.sqrt(n)
    
    return autocorrs, ci
```

**解读**：
- 所有 lag 都在 ±ci 内 → **不可预测**，别做方向性交易，专注做市
- lag-1 显著为负 → **短期均值回复**，可以考虑逆向交易
- lag-1 显著为正 → **短期动量**，taker 有 informed signal

Frankfurt Hedgehogs 对 Kelp 的结论是：不可预测。对 Volcanic Rock：显著负自相关 → 均值回复。

### 2. 对比随机序列的 Autocorrelation

单看自相关可能有偶然性。生成 1000 组同长度的随机 return，画出自相关的分布带：

```python
def autocorr_significance(returns, n_simulations=1000, max_lag=10):
    """真实 autocorr vs 随机 baseline"""
    real_ac = [np.corrcoef(returns[:-l], returns[l:])[0, 1] 
               for l in range(1, max_lag + 1)]
    
    sim_acs = []
    for _ in range(n_simulations):
        fake = np.random.normal(np.mean(returns), np.std(returns), len(returns))
        sim_ac = [np.corrcoef(fake[:-l], fake[l:])[0, 1] 
                  for l in range(1, max_lag + 1)]
        sim_acs.append(sim_ac)
    
    sim_acs = np.array(sim_acs)
    p5, p95 = np.percentile(sim_acs, [5, 95], axis=0)
    
    return real_ac, p5, p95  # 真实值落在 band 外 = 显著
```

### 3. 成交方向 vs 后续价格变动

这是判断 **adverse selection** 的核心：

```python
def adverse_selection_test(trades_df, wall_mid_series, horizons=[1, 5, 10, 50]):
    """
    每笔成交后 h 步，价格朝成交方向移动了多少？
    正值 = taker 有信息（adverse selection 存在）
    零 = taker 无信息
    负值 = taker 是噪声（做市有利）
    """
    results = {}
    for h in horizons:
        pnl_after = []
        for _, trade in trades_df.iterrows():
            t = trade.timestamp
            if t + h >= len(wall_mid_series):
                continue
            direction = 1 if trade.quantity > 0 else -1  # buyer=+1, seller=-1
            move = wall_mid_series[t + h] - wall_mid_series[t]
            pnl_after.append(direction * move)
        results[h] = np.mean(pnl_after)
    return results
```

**解读**：
- 所有 horizon 的均值 ≈ 0 → taker 无信息，放心做市（Kelp 的情况）
- 短 horizon 显著为正 → taker 有信息，你被动挂单会亏钱
- 按交易者 ID 分组做这个分析 → 找出谁是 informed trader

### 4. Bot 行为模式检测

```python
def detect_patterns(trades_df, product):
    """
    寻找固定数量、固定时间、固定价格模式的 bot
    """
    t = trades_df[trades_df.product == product]
    
    for trader in t.buyer.unique():
        trader_trades = t[t.buyer == trader]
        
        # 固定数量？
        qty_unique = trader_trades.quantity.nunique()
        qty_mode = trader_trades.quantity.mode().values
        
        # 固定时间间隔？
        timestamps = trader_trades.timestamp.sort_values().diff().dropna()
        ts_std = timestamps.std()
        
        # 和 daily extreme 的关系？
        # 在每天的 trades 中，该 trader 的成交价是否接近 daily min/max
        
        print(f"Trader {trader}: {len(trader_trades)} trades, "
              f"qty modes={qty_mode}, timestamp regularity={ts_std:.1f}")
```

---

## 三、你应该生成的完整指标清单

按重要性排序：

| 指标 | 计算方式 | 回答什么问题 |
|------|---------|-------------|
| **Wall Mid** | bid/ask 侧最大挂单量价位的均值 | true price 是多少 |
| **Wall Spread** | ask_wall - bid_wall | 做市的理论最大 edge |
| **Raw Spread** | best_ask - best_bid | 挂单空间有多大 |
| **Return ACF(1-20)** | wall_mid return 的自相关 | 价格可预测吗 |
| **Adverse Selection** per trader | 成交后价格移动 | 谁是 informed |
| **Trade Arrival Rate** | 每 N 步的成交笔数 | 流动性多高 |
| **Fill Rate at wall** | wall 价位被吃掉的比例 | 挂在 wall 上的风险 |
| **Inventory path** | 如果你做市，仓位如何演变 | 需不需要积极去库存 |
| **Daily pattern** | 各统计量按日内时间分组 | 一天内有无结构 |
| **Cross-day stability** | 各统计量跨天的方差 | 策略能否跨天稳定 |

---

## 四、研究流程模板

打开一个 notebook，对每个产品按顺序做：

```
Step 1: 画 Orderbook 散点图 → 肉眼判断稳定/漂移/跳跃
Step 2: 计算 Wall Mid → 叠加到图上 → 确认是否是好的 fair
Step 3: 画 Normalized 图 → 看 spread 结构
Step 4: Return ACF → 判断可预测性
Step 5: 成交分类 → 找 bot pattern
Step 6: Adverse Selection → 判断做市是否安全
Step 7: 用一段话写下策略逻辑
Step 8: 实现 → 回测 → 提交
```

Step 1-6 应该在**2-3 小时内**对一个产品完成。如果花更长时间，说明工具不够自动化——把上面的函数封装成一个 `analyze(product, day)` 一键跑完。

---

## 五、和你现有因子的对应关系

| 你已经知道的 | 在 IMC 中的对应 | 需要注意的 |
|-------------|---------------|-----------|
| 厚度 (depth) | Wall 的挂单量 | IMC 只有 1-4 档，看 wall 比看总 depth 有用 |
| 弹性 (resilience) | 不太适用 | 订单每步重置，没有"恢复"的概念 |
| Microprice | Wall Mid 更好 | Microprice 被 penny 的 bot 污染 |
| Order flow imbalance | 有用但要小心 | 看成交方向的 imbalance，不是挂单 imbalance |
| VPIN / toxicity | Adverse Selection 测试代替 | 直接测成交后价格移动更直接 |
| 波动率 | Return 的 rolling std | 主要用于判断 spread 是否够宽来做市 |

关键区别：真实市场的很多因子是为了应对**延迟和竞争**设计的。IMC 没有延迟竞争，所以你不需要预测下一个 tick 的价格（因为你看到的已经是最新的）。你需要的是**理解当前 snapshot 中每个价位和每笔成交的含义**。
