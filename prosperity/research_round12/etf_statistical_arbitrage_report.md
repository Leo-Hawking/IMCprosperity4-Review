# IMC Prosperity — ETF 统计套利完整复盘

> 为 Prosperity 4 Round 3 准备的策略研究文档
>
> 覆盖仓库: Frankfurt Hedgehogs (P3 #2), CMU Physics / chrispyroberts (P3 #7 全球/#1 美国), Alpha Animals / CarterT27 (P3 #9 全球/#2 美国), Sylvain-Topeza (P3 top 1%), Martin Oravec (P3 #73 单人), Linear Utility (P2 #2), jmerle (P2 #9), pe049395 (P2 #13), Stanford Cardinal (P1 #2)
>
> 日期: 2026-04-23

---

## 目录

0. [一句话总结](#0-一句话总结)
1. [比赛机制与 ETF 题目结构](#1-比赛机制与-etf-题目结构)
2. [核心问题诊断: 为什么这题不是它看起来的样子](#2-核心问题诊断-为什么这题不是它看起来的样子)
3. [各队策略与心路历程](#3-各队策略与心路历程)
    - [3.1 Frankfurt Hedgehogs (P3 #2)](#31-frankfurt-hedgehogs-p3-2)
    - [3.2 CMU Physics (P3 #7)](#32-cmu-physics-p3-7)
    - [3.3 Alpha Animals (P3 #9)](#33-alpha-animals-p3-9)
    - [3.4 Sylvain-Topeza (P3 top 1%)](#34-sylvain-topeza-p3-top-1)
    - [3.5 Martin Oravec (P3 solo #73)](#35-martin-oravec-p3-solo-73)
    - [3.6 Linear Utility (P2 #2)](#36-linear-utility-p2-2)
    - [3.7 jmerle (P2 #9)](#37-jmerle-p2-9)
    - [3.8 pe049395 (P2 #13)](#38-pe049395-p2-13)
    - [3.9 Stanford Cardinal (P1 #2)](#39-stanford-cardinal-p1-2)
4. [策略对比总表](#4-策略对比总表)
5. [七大关键决策点 (每队都面对过)](#5-七大关键决策点-每队都面对过)
6. [踩过的坑 (所有人都可能再踩)](#6-踩过的坑-所有人都可能再踩)
7. [对 P4 Round 3 的实操建议](#7-对-p4-round-3-的实操建议)
8. [附录: Frankfurt Hedgehogs 核心代码解读](#8-附录-frankfurt-hedgehogs-核心代码解读)

---

## 0. 一句话总结

**ETF 套利题的本质不是"套利"**，而是: 在 basket 的 orderbook 上通过阈值化的 mean-reversion 策略去吃 spread 的回归，constituent 在策略里扮演的是"合成公允价"的参考锚 —— 而不是需要被真正对冲的腿。

**顶级队和中游队的分水岭在三件事**:

1. **合成价用什么价计算** — 用 raw mid 会被小单骚扰；顶级队都改用了 **Wall Mid**（orderbook 上大单所在的 bid-ask 中点），这是整份文档最重要的单点技巧。
2. **Premium 是移动的还是固定的** — 所有顶级队都发现 spread 均值不是 0，而是一个非零的"basket premium"，并且这个 premium 在轻微漂移。直接用 z-score 会把 premium 漂移算进信号里导致误开仓。Frankfurt 的做法是**用增量均值持续更新 premium 估计**。
3. **对冲到底做不做** — P2 的 Linear Utility 做了对冲；P3 的 Frankfurt 论证了**完全对冲反而降 EV**（因为 constituent 不回应 basket），最终选 **50% 对冲** 作为 variance/EV 折中。

**P3 还有个外挂信号**: 一个叫 **Olivia** 的 bot 在 CROISSANTS 上几乎精准买在日低、卖在日高。识别 Olivia 可以把普通 basket 套利的 60k 收益变成 100k+。Round 5 之前是隐藏的（只能通过 size=15 的 trades 在极值识别），Round 5 之后 trader_id 公开。

---

## 1. 比赛机制与 ETF 题目结构

### 1.1 产品结构 (P3 实际数据)

| 产品 | 组成 | Position Limit |
|---|---|---|
| PICNIC_BASKET1 | 6 CROISSANTS + 3 JAMS + 1 DJEMBE | 60 |
| PICNIC_BASKET2 | 4 CROISSANTS + 2 JAMS | 100 |
| CROISSANTS | - | 250 |
| JAMS | - | 350 |
| DJEMBES | - | 60 |

**关键机制**: Basket **不能**真正地转换成/分解成 constituent，两者是完全独立的 orderbook。这意味着所谓"套利"不是真无风险套利，而是 **statistical arbitrage on the spread**（基于均值回复的统计套利）。

### 1.2 历年版本对比

| 年份 | Basket | 组成 | 位置约束 |
|---|---|---|---|
| P1 (2023) | PICNIC_BASKET | 1 UKULELE + 2 BAGUETTE + 4 DIP | 70 / 70 / 150 / 300 |
| P2 (2024) | GIFT_BASKET | 1 ROSES + 4 CHOCOLATE + 6 STRAWBERRIES | 60 / 60 / 250 / 350 |
| P3 (2025) | PICNIC_BASKET1 + PICNIC_BASKET2 | 见上 | 见上 |
| P4 (2026) | **极可能再出一次 basket 类型题** | **待定** | **待定** |

**Prosperity 每年都会出这类 ETF 套利题**，题面几乎不变，组成数字微调。P3 额外搞了"两个 basket 共享 constituents"的新结构，需要共享 position limit 的精细化管理。

### 1.3 订单撮合机制的关键前提

所有顶级队都反复强调过这点 (Frankfurt Hedgehogs 的原话):
- 每个 timestep 先清空上一 tick 的所有未成交 order
- 然后按固定顺序处理: deep-liquidity maker → takers → 你的 bot → 其他 bots
- 你能看到完整的 orderbook snapshot，**不需要抢速度**
- 没有部分成交风险，只有是否被撮合到的风险

含义: **所有问题都是信号质量 + EV 最大化问题，不是 HFT 延迟问题**。

---

## 2. 核心问题诊断: 为什么这题不是它看起来的样子

这是几乎所有顶级队都独立发现并强调的一点，也是中游队和顶级队拉开差距的地方。

### 2.1 反面教材: "教科书式"的 ETF 套利思路

一个没思考过的人看到 basket + constituents 的反应通常是:
1. 算合成价 = 6*C + 3*J + 1*D
2. 算 spread = basket - synthetic
3. 求 spread 的 z-score (除以滚动标准差)
4. z > 2 做空 basket + 做多 constituents；z < -2 反向；z 回到 0 平仓
5. 完美对冲后就是"无风险" stat arb

**这套打法在 P3 里实测只能拿中等分数 (20k-60k)。顶级队拿到 100k+ 需要打破其中 3 个"常识"。**

### 2.2 关键洞察: 数据是怎么"生成"的

Frankfurt Hedgehogs 给出的思路 (这段值得原文抄录理解):

> "不要盲目套用教科书策略，而是要先问一个基础问题: 市场数据有可能是怎么生成的? 最自然的生成过程似乎是 —— 三个 constituent 价格独立随机游走，然后在 basket 价格上叠加一个 mean-reverting 的噪声序列。如果这假设成立 —— 我们早期测试也支持这点 —— 那么 basket 是相对于合成价均值回复的，而 constituent 并不会响应 basket 的变动。因此应该把 basket 当作'向合成价漂移'来处理，而不是反过来。"

这带来三个直接推论:

**推论 A: 不要交易 constituent 的均值回复信号**。constituent 本身不是均值回复的，是 random walk。你对 basket 做 stat arb 时，constituent 只是"用来定义合成价"的参考数据。

**推论 B: 完美对冲会降低 EV**。对冲意味着在 constituent 上开反向仓位。但 constituent 不向 basket 收敛（relation 是单向的），你付了 spread cost 却没拿到对应的回归收益。Linear Utility (P2 #2) 做了完美对冲，Frankfurt (P3 #2) 论证了只做 50% 对冲更优。

**推论 C: Spread 的均值不是 0**。因为 basket 长期存在一个 positive premium（玩家愿意为"打包"多付钱），这个 premium 本身还会轻微漂移。**直接用 0 作为均值做 z-score 会系统性地错开方向**。

### 2.3 Wall Mid: 所有顶级队都用的定价锚

这是整份文档里**最重要的单点技巧**，也是中游队到顶级队的分水岭。

**问题**: 市场上的 mid = (best_bid + best_ask) / 2 经常会被小单污染。例如有小单把 best_bid 从 1000 推到 1001，mid 就跳了 0.5；但真实的"公允价"没有变化。

**解决**: 找到 orderbook 上最深那一层（"wall"），取 wall_bid 和 wall_ask 的中点。

**为什么有效** (Frankfurt 原话):
> "在 Prosperity 网站测试时，我们发现可以通过买/卖 1 手并观察盈亏来反推'真实底层价格'。经过仔细分析，我们发现最稳健的真实价估计方法是识别 bid wall 和 ask wall —— 这些深度稳定存在的价位，通常对应着那些'知道真实价格'的做市 bot 报价（围绕真实价 ±2 tick）。把两者平均就是 Wall Mid，远比 raw mid 稳定。"

**pe049395 (P2 #13) 独立发现了同样现象**:
> "我们发现 IMC 用一个隐藏的 fair value 在算 PnL。同时，bid 和 ask 两侧总是有一层大单。这两个价格的平均值非常接近隐藏 fair value。于是我们围绕它交易。"

**核心代码模式** (Frankfurt 的实现):
```python
def get_walls(self):
    bid_wall = min(self.mkt_buy_orders.keys())   # 最深的 bid
    ask_wall = max(self.mkt_sell_orders.keys())  # 最深的 ask
    wall_mid = (bid_wall + ask_wall) / 2
    return bid_wall, wall_mid, ask_wall
```

（注意: Frankfurt 用 `min`/`max` 是因为对于只有几档的 orderbook 来说，最极端那档通常就是最深的单位。如果 orderbook 很宽，更稳妥的写法是按 volume 找 max-volume 那档。）

### 2.4 Running Premium: 防止 z-score 被漂移污染

一旦用了 Wall Mid，下一个问题是: **spread 的均值是多少?**

Frankfurt 的解: **增量均值更新**，用很大的先验样本数 (n_hist_samples=60000) 给历史均值很大权重，这样活跃期 premium 的漂移不会把当前估计拉偏太多，但长期还是能跟上漂移。

```python
# 初始值来自历史数据
mean_premium, n = INITIAL_ETF_PREMIUMS[b_idx], 60_000

# 每 tick 增量更新
n += 1
mean_premium = mean_premium + (raw_spread - mean_premium) / n

spread = raw_spread - mean_premium  # 这个 spread 才是用来做信号的
```

**为什么不直接用滚动窗口均值**: 窗口太短会把当期信号当成均值，窗口太长又跟不上漂移。增量均值 + 大先验等价于一个非常"黏"的 EMA，对突发漂移有强抗性但长期会收敛。


---

## 3. 各队策略与心路历程

### 3.1 Frankfurt Hedgehogs (P3 #2)

**成绩**: P3 Global #2, 基本全程领跑，最后一轮被 Heisenberg 的 850k 单轮奇迹反超。ETF 上贡献 40k–60k/round，加上 Olivia 信号带动 CROISSANTS 再贡献 20k/round。

**核心策略**: 基于 Wall Mid 的固定阈值 mean-reversion + Running Premium + Olivia 信号调整阈值 + 50% 对冲。

**完整算法分解**:

1. **算 spread** (见 §2.3-2.4)
   ```
   index_price = 6*C.wall_mid + 3*J.wall_mid + 1*D.wall_mid   # basket 1
   raw_spread = basket.wall_mid - index_price
   mean_premium = 增量更新
   spread = raw_spread - mean_premium
   ```

2. **Olivia 信号叠加**: 识别 CROISSANTS 上的 Olivia 方向 (LONG/SHORT/NEUTRAL)，把 basket 阈值整体偏移:
   ```
   base_thr = [80, 50]                    # basket 1 / 2 的对称阈值
   informed_adj = ±90 (取决于 Olivia 方向)
   开多 basket  iff  spread < -base_thr + informed_adj
   开空 basket  iff  spread > +base_thr + informed_adj
   ```
   注意 `±90 > 80`，意思是 **如果 Olivia 看多 CROISSANTS，几乎任何 spread 都会触发做多 basket 的条件**，因为 CROISSANTS 占 basket 50% 权重，Olivia 把 CROISSANTS 打到日低就意味着 basket 也处在相对低点。

3. **平仓逻辑 ETF_CLOSE_AT_ZERO = True**: 不等 spread 穿越到反向阈值才平，而是 **穿过 0 (+ informed_adj) 就立刻平**。这是为了降 variance（Round 5 之前他们是等反向阈值才平的，Round 5 特地改成穿 0 平）。

4. **CROISSANTS 本身**: 完全不做 basket 的对冲用途，而是直接当成 Olivia 信号的执行工具，跟随 Olivia 打满 ±250。

5. **JAMS & DJEMBES**: 做 50% 对冲。
   ```
   expected_hedge_position_for_JAMS =
       -basket1.expected_position * 3 * 0.5     (因为 basket1 含 3 jams)
       -basket2.expected_position * 2 * 0.5     (因为 basket2 含 2 jams)
   ```

**决策亮点 — 为什么 50% 对冲**:

> "虽然对 constituent 开反向仓位能降 variance，但由于 constituent 不响应 basket，这会小幅降低 EV。任何交易策略都可以看作两个策略的线性组合 —— 这里就是 fully hedged 和 fully unhedged。我们选 50% 作为平衡折中。"

**心路历程里最值得留意的部分**:

- **参数选择的哲学**: "在做参数选择时，我们始终优先考虑 landscape 稳定性而非纯粹的性能峰值。我们不挑 backtest PnL 最高的参数，而是挑 PnL 相对平坦的区域 —— 这降低了对参数漂移的敏感性，避免了过拟合灾难。"
- **对"洒技术"的警惕**: "很容易掉入陷阱，跑几小时 backtest 后就开始往问题上堆花哨技巧。但如果你不能从第一性原理解释策略为什么应该赚钱，那历史数据里的'超额收益'大概率是噪声。"
- **Round 5 的保守调整**: 此时他们领先第二名 ~190k，不想让 spread 回归失败毁掉冠军位，于是 **把 basket 对冲改成 50%**（之前更激进的版本对冲 < 50%），并缩减了 mean reversion 仓位。

---

### 3.2 CMU Physics (P3 #7, chrispyroberts)

**成绩**: P3 #7 全球 / #1 美国。Round 2 算法 ETF 赚 ~100k，Round 5 靠 Olivia 信号把 CROISSANTS 策略升级为 YOLO 后赚更多。

**核心策略**: **trade-the-premium-diff** — 他们和 Frankfurt 最大的不同是：没有单独对每个 basket 做 mean-reversion，而是 **交易两个 basket premium 之间的差**。

**原文心路**:
> "我们发现 basket premium 是均值回复的，于是从 sample data 硬编码一个均值、用短滚动窗口算 std，用 rolling z-score 做信号:
> - z > 20 → 做空 basket + 做多 constituents
> - z < -20 → 反向
>
> 这样能把 basket premium 单独剥离出来交易。但我们遇到了**position limit 限制**，没法同时完全对冲两个 basket。"

**解决方案** (这是他们方案的独特点):

1. **交易 Basket1 premium - Basket2 premium 的 z-score**: 这个策略对两个 basket 都有敞口，但成分风险大部分互相抵消（因为两者都主要含 CROISSANTS/JAMS）。
2. **仓位分配**:
   - Basket 1: 用 100% position limit
   - Basket 2: 用 60% 做 premium-diff 交易
   - 剩 32% 的 Basket 2 position 做单独 z-score 交易 (limited by constituent hedging)
   - 剩 8% 的 Basket 2 position 做 market making (basket 的 spread 有 7-10 seashell 宽)
3. **结果**: 100% 使用了 position limit，market making 这部分再 +5k/day。

**Round 5 最关键的调整 — "YOLO Croissants"**:
> "既然我们有 CROISSANTS 上 Olivia 的真信号，而 CROISSANTS 又占 basket 价格的 50%，那就**不要**在 Olivia 信号反方向开 basket 头寸。"

更激进的: 如果两个 basket 都做多，等效就是在 CROISSANTS 上持有 6*60+4*100 = 760 手。加上 CROISSANTS 本身 250 limit，直接长到 **1010 手 CROISSANTS 的有效暴露**。他们的论证:
- 坏日子里 CROISSANTS 日内振幅 40 SeaShells
- 多持 800 CROISSANTS 意味着最坏 loss ~32k
- basket stat-arb 单独最多 50k/day
- YOLO CROISSANTS 最好能做到 120k/day
- → **把 basket stat arb 降级为 Olivia 信号的执行工具**

**Basket Premium 的风险计算**:
> "我们也发现会对 basket premium 本身有敞口。最坏情况是：在 premium 顶买入再在 premium 底卖出，每个 basket 最多亏 ~300 SeaShells，总敞口 45,000 SeaShells。
>
> Chris 发现: basket2 premium 的一阶差分在 90% 置信度下 stationary，basket1 在 95%。这意味着'premium 反向运动的概率'是抛硬币，我们正好买在顶/卖在底的概率极小。现实最大 loss 估计 20k，期望 loss 为 0。我们认为这风险值得。"

**其他值得借鉴的心路**:

- Round 2 结束时他们用 Monte Carlo 模拟达到 Nash 均衡来解 manual，其中注意到 "人类对 37 这个数字有偏爱" 的心理偏好作为先验。
- Round 3 他们从 7th 跌到 241st，发现 **Jasper 可视化器在提交时占用 >100MB 触发 AWS Lambda 重启**，所有 rolling window 被重置。后来索性删掉可视化器依赖。

**可以直接复制的关键技巧**:
```python
# 他们的 premium diff z-score (伪代码)
b1_premium = basket1.wall_mid - (6*C + 3*J + 1*D)
b2_premium = basket2.wall_mid - (4*C + 2*J)
premium_diff = b1_premium - b2_premium

# rolling std with small window (~30-60 ticks)
std = rolling_std(premium_diff_history, window=30)
z = (premium_diff - hardcoded_mean) / std

if z > 20:  # 注意这个阈值很大 —— 因为 std 非常小的时候 z 会 spike
    short basket1, long basket2 (& hedge)
```

---

### 3.3 Alpha Animals (P3 #9, CarterT27)

**成绩**: P3 #9 全球 / #2 美国 (峰值做到过 2nd Global)。但 **ETF 策略一直没跑起来**，他们最终的 9th 排名主要靠其他轮。

**他们 ETF 策略失败的心路**:

> "我们实现了一个 stat arb 策略。用一个 linear model 加权 constituent 算 basket 的 fair value，当 basket market price 显著偏离合成价时就交易。然而，由于一个 bug 让我们的代码尝试买超过 position limit 的量，我们的 stat arb 策略根本没正常运行。我们决定把精力放在其他产品上而不是 debug 这个 bug，所以这一轮没主动交易成分产品。"

**教训**: Position limit 管理是这类策略的第一道坎。他们的 bug 就是在 "computed intended position > limit" 时没做 clamp，导致订单被系统全部拒绝。

**他们放弃的其他方向** (对 P4 选手有启示价值):
1. **Volatility surface fitting for options** — 太复杂，时间不够。
2. **Component-Basket direct arbitrage** — 代码写了但因 bug + position limit 问题没跑通。
3. **Price Pattern Analysis** — 给几乎每个产品找季节性/相关性，**大部分没 actionable 结果**。
4. **Insider Signal as regime indicator** — 把 Olivia 方向作为调整 bid/ask 的 regime 参数（不是直接 copy trade），**比直接 copy trade 复杂但效果不如它**。这和 Frankfurt 的做法相反 —— Frankfurt 成功地用 Olivia 作为阈值偏移，而 Alpha Animals 没做出来，原因可能是 Frankfurt 有 Running Premium 做底层，Alpha Animals 没有。

**他们真正的 Code bug 类型**:
- Position limit 未检查 → 订单被拒
- Conversion 语义理解错 → Round 4 在 Macarons 上亏掉 conversion fee

---

### 3.4 Sylvain-Topeza (P3 top 1%)

**核心打法的差异化点**: **Basket 1 和 Basket 2 用不同策略**。

> "Basket 1 用传统的 stat arb: 持续监控 basket mid 和理论合成价的 spread，premium 显著 → 做空，discount 显著 → 做多。
>
> **Basket 2 完全不做相对 constituent 的套利**。我们纯粹依靠 Olivia 驱动的信号。我们把 Olivia 在 CROISSANTS 上的 flow 直接扩展到 basket 2 的执行。这个 informed flow-following 给了我们比纯统计打法更稳定的 edge。"

**这个思路的本质**: 当信号源足够强时，**放弃自己对冲**，直接 "copy the informed trader"。对 Basket 2 来说，它只含 CROISSANTS + JAMS，和 CROISSANTS 的相关度比 Basket 1 还高（权重 4/6 vs 6/10），所以跟着 Olivia 信号做 Basket 2 比做 stat arb 更直接。

---

### 3.5 Martin Oravec (P3 solo #73)

**成绩**: 单人参赛 P3 第 73 名 / UK 第 5 名。

**策略细节**:
> "我从 constituent mid 构造 synthetic fair price 和 basket mid 对比。当 spread 和 z-score 足够大时，交易 basket vs constituents，消化偏离。我也反过来，用 basket 给 constituent 定价，这样两边互相拉回平衡。"

**Round 4 之后的灾难**:
> "我犯了 Round 4 的大错。我继续用 basket 策略，但注意到自从引入 basket 以来，我的策略一直在跌。我选择了'数据驱动'的决定 —— 这个策略过去 3 轮都赚钱，所以继续用。**我没意识到市场已经朝我的策略处理不了的方向走，而且它不会停下**。结果最后一轮我第一次在 basket 上亏钱。"

**教训**: 
- **Backtest pass ≠ live pass**。当实盘开始连续异常时，别用"历史胜率"来推理当下。
- Basket spread 的"稳定 premium"假设可能被对手行为打破，尤其是 Round 5 多人都在 copy Olivia 时，Olivia 反向移动 CROISSANTS 会把 basket premium 打到历史未见的范围。

**Round 5 的调整**: 他把 options 策略大幅简化 (per-strike IV rolling avg 150-tick + ±1 SD 进场带)，basket 策略反而坚持没变 —— 结果 basket 就是亏的那一环。

---

### 3.6 Linear Utility (P2 #2)

**这是 ETF 套利题的开山 writeup**，P2 里 GIFT_BASKET = 4 CHOCOLATE + 6 STRAWBERRIES + 1 ROSES。

**他们的核心洞察 (Round 3)**:

1. **两个假设竞赛**:
   - 假设 A: synthetic 领先 basket (或反过来) 的 lead-lag 关系
   - 假设 B: spread 均值回复
   
   他们研究了 lead-lag 没找到东西，转向假设 B 成功。

2. **spread 均值不是 0，是 ~370** (P2 数据):
   > "看 spread，我们发现价格在三天历史数据里围绕 ~370 震荡。因此可以交易 mean-reverting 策略: spread < 平均时买 spread (做多 basket + 做空 synthetic)，spread > 平均时卖 spread。"

3. **演化路径**:
   - v1: 硬编码阈值 (spread < 平均 - X 买, spread > 平均 + X 卖) → backtest 120k
   - v2: **Modified z-score**, 用 **hardcoded mean** (长期均值) + **rolling std with small window** (短期波动率) → backtest 135k
   
   为什么这么组合?
   > "spread 的均值背后应该有 fundamental 原因（比如 basket 自身的溢价），但每天的波动率本身就不稳定，难以预测。"
   
   **小滚动窗口算 std 的意外效果**: 当价格开始反转时，std 会突然下降 → z-score spike → 正好在局部极值点触发交易。**小窗口 std 成了反转点的近似检测器**，这是一个很漂亮的副作用。

4. **实盘结果**: 111k 实际 vs 135k backtest，**有显著滑点**，但其他队过拟合更严重，所以他们还是拿了 #2。

**给 P3/P4 的启发**: LU 的 z-score with small-window std 是一个比 Frankfurt 的固定阈值更激进但信号更密的版本。理论上两者可以互补: Frankfurt 的固定阈值适合拥有 Olivia 信号的场景（不需要精准开仓点），LU 的小窗口 z-score 适合没有 informed trader signal 的场景（需要精准在 spread 反转时刻开仓）。

---

### 3.7 jmerle (P2 #9)

**身份**: Prosperity 社区最著名的开源工具作者 (backtester + visualizer)。单人队伍排名 #9 globally。

**Round 3 的心路**:
> "Round 3 算法明显是去年 Round 4 的抄袭。我从建立一个基于 basket value 和 synthetic value 差的策略开始。当差值穿越某些阈值时策略 100% long 或 100% short basket。我后来扩展了它去 mirror position 在 constituent 上（即做对冲），效果还可以（但过拟合）。
>
> 听说其他参赛者在 backtest 里能做到 600k，我没搞出这个，决定让 basket 和 constituents 在不同阈值交易，然后用 grid search 在 30k iterations 的 example data 上找好的阈值。除了 roses 我所有产品都赚，roses 亏了 36k+。之后就禁用 component trading，觉得风险与潜在回报不成比例。"

**教训**: **对冲所有 constituent 可能引入 idiosyncratic risk**。如果某一个 constituent 有自己的不相关风险（像 P2 的 ROSES 有 Olivia 信号），盲目对冲反而会制造亏损点。这条在 P3 被 Frankfurt 升级为"只对冲 JAMS + DJEMBES，不对冲 CROISSANTS（因为 CROISSANTS 有 Olivia 信号独立交易）"。

---

### 3.8 pe049395 (P2 #13)

**关键洞察** (这条非常有用):
> "GIFT_BASKET 价格围绕一个 underlying index 震荡，**所以我们只交易 GIFT_BASKET，不交易 constituents**。"

他们 **完全不对冲**，只交易 basket。他们的理由:
- Backtester PnL vs IMC 网站 PnL 有差 → IMC 用隐藏 fair value 算 PnL
- bid 和 ask 两侧都有大单层 → 两者平均 = 隐藏 fair value
- 他们围绕这个 "average of two big levels" 来交易 basket

**另一个非常有用的原则**:
> "交易 GIFT_BASKET 时，我们 **把市价单打深到订单簿里**，接受滑点来立刻抓住交易机会。"

**对 P4 的启发**: 当你识别出一个 basket mispriced 时，不要只挂 top-of-book 等 fill，**直接吃深层订单**。Basket 的 spread 本身就有 7-10 SeaShells，mean reversion 的回归收益有 50-100 SeaShells，付几个 tick 的 slippage 换确定成交是划算的。

---

### 3.9 Stanford Cardinal (P1 #2)

**P1 里 PICNIC_BASKET = 1 UKULELE + 2 BAGUETTE + 4 DIP**。Premium = **固定 375**。

**他们的核心观察** (引领了后来所有 ETF 策略):
> "在 Picnic Basket bucket 里，Picnic Basket **一直有重偏正的 PnL**。这意味着所有 constituent 的价格都是 basket 未来价格的 leading indicator，这很 make sense。所以我们决定 **只交易 PICNIC_BASKET**，信号是 (basket - 4\*DIP - 2\*BAGUETTE - 1\*UKULELE - 375)。"

**关键决策**: 
- 不交易 DIP
- 不交易 BAGUETTE  
- 用 Olivia 信号专门交易 UKULELE
- 其他信号增强 berries

**对 P4 的最大启示**: **"不交易某个 constituent"本身就是策略**。
- P1: 不交易 DIP / BAGUETTE，只交易 UKULELE（因为 Olivia 在它身上）
- P2: jmerle 因 ROSES 亏钱后不再对冲 constituent
- P3: Frankfurt 不对冲 CROISSANTS（因为 Olivia 在它身上）
- **在 ETF 类题里，constituent 只应该单独交易当它有 independent alpha signal 时**

---


## 4. 策略对比总表

| 队伍 | 年份 | 排名 | 公允价 | Premium 处理 | 信号类型 | 阈值/z-score | 对冲 | 特殊技巧 |
|---|---|---|---|---|---|---|---|---|
| Frankfurt Hedgehogs | P3 | #2 | **Wall Mid** | **增量均值 (n=60k 先验)** | 固定阈值 + Olivia 调整 | 80 / 50 (B1/B2) | **50%** | Olivia 通过 CROISSANTS 偏移 ±90 阈值 |
| CMU Physics | P3 | #7 | mid | hardcoded mean | **rolling z-score of (B1 premium - B2 premium)** | 20 | Full (受 pos limit 限制) | 剩余 position market-make basket |
| Alpha Animals | P3 | #9 | mid | linear model | spread deviation | - | 尝试 Full (没跑起来) | - |
| Sylvain-Topeza | P3 | top 1% | mid | - | **B1 stat arb + B2 纯跟 Olivia** | - | - | 不同 basket 不同策略 |
| Martin Oravec | P3 | #73 solo | mid | mean & z-score | 双向 (basket→const, const→basket) | - | Full | - |
| Linear Utility | P2 | #2 | "market maker mid" | **hardcoded mean + rolling std (small window)** | modified z-score | z spike | Full | 小窗口 std 自动检测反转 |
| jmerle | P2 | #9 | popular buy/sell mid | hardcoded threshold | basket 和 const **不同阈值** | grid search | Full then no | Round 3 后禁用 const trading |
| pe049395 | P2 | #13 | **Wall Mid equivalent** ("average of two large levels") | around underlying index | - | - | **None, basket only** | 打深订单簿, 接受滑点 |
| Stanford Cardinal | P1 | #2 | mid | **hardcoded premium 375** | threshold | fine-tuned | None on DIP/BAGUETTE | **只交易 basket + 独立交易 UKULELE (Olivia)** |

**几个横向规律**:

1. **P1→P2→P3 策略演进**: 越往后 premium 处理越复杂 (固定→滚动→增量), 对冲比例越低 (100%→50%→0%), informed signal 使用越精细。
2. **Wall Mid 的发现是平行的**: Frankfurt (P3) 和 pe049395 (P2) 独立发现了同一个概念。这应该作为"起手式"。
3. **阈值参数分散极大**: 20 (CMU), 80/50 (Frankfurt), "spread deviation" (LU), 375 premium offset (Stanford)。说明 **这些参数都要在自己的 backtester 上 grid search**，别直接 copy。

---

## 5. 七大关键决策点 (每队都面对过)

这七个点是每支队伍在做 ETF stat arb 时必然要回答的问题。我把**不同答案下的代表性选择 + 各自理由**列出来，这样你可以直接用它作为自己的 decision checklist。

### 5.1 用什么价格算合成价?

| 选项 | 代表队 | 理由 |
|---|---|---|
| raw mid = (best_bid + best_ask)/2 | 中游队 | 最简单，但容易被小单污染 |
| **Wall Mid / Popular mid** | **Frankfurt, pe049395, jmerle** | 抗小单骚扰，接近 IMC 隐藏 fair value |
| microprice (volume-weighted) | 一些实验性队 | 理论上最优但实现容易出 bug |

**推荐**: Wall Mid。这是最稳的选择。

### 5.2 Premium 均值怎么估?

| 选项 | 代表队 | 场景 |
|---|---|---|
| 硬编码 0 (假设 spread 回归 0) | 中游队 | ❌ 几乎总是错，因为 basket 有正 premium |
| 硬编码长期均值 (从历史数据) | Linear Utility, CMU, Stanford | 适合: premium 稳定 |
| **滚动均值 (EMA/rolling)** | Martin Oravec | 适合: premium 漂移 |
| **增量均值 + 大先验 n** | **Frankfurt** | 兼顾两者: 长期稳定, 短期抗漂移 |

**推荐**: 增量均值 + 大先验 n (Frankfurt 的做法), 先验用历史数据算。

### 5.3 什么时候开仓?

| 选项 | 代表队 | 优缺点 |
|---|---|---|
| 固定绝对阈值 | Frankfurt, Stanford | 简单鲁棒，但对不同 vol regime 适应性差 |
| Fixed z-score with long-window std | 中游队 | 对波动率 regime 有适应性 |
| **z-score with short-window std** | **Linear Utility, CMU** | 小 std 时 z spike → 自动反转检测 |

**推荐**: 先实现固定阈值 (因为稳定)，如果有时间再实现短窗口 z-score (因为信号更多)。

### 5.4 什么时候平仓?

| 选项 | 代表队 | 优缺点 |
|---|---|---|
| 等反向阈值 | 中游队, Linear Utility (v1) | 吃满回归但 variance 大 |
| **穿过 0 立即平** | **Frankfurt (Round 5)** | 降 variance, 期望 EV 等价 |
| z 回到 ±1 (或其他小阈值) | 一些队 | 折中 |

**推荐**: 穿过 0 平仓。原因: Frankfurt 的论证是 "假设 spread 穿 0 时没有动量，平仓在 0 的 EV 等价于等反向阈值，但 variance 低很多"。

### 5.5 对冲不对冲?

| 选项 | 代表队 | 场景 |
|---|---|---|
| 100% 对冲 (long basket, short constituents) | Linear Utility, Martin | ETF=synthetic 的假设严格成立时 |
| **50% 对冲** | **Frankfurt (P3 Round 5)** | constituent 不完全响应 basket 时 |
| **0% 对冲** | **pe049395, Stanford Cardinal** | 明确知道 constituent 不响应 basket 时 |
| 对冲但排除有 Olivia 信号的 constituent | Frankfurt, Stanford | 有 informed trader 时 |

**推荐**: 0%–50% 对冲，绝对不要 100%。优先考虑排除有 signal 的 constituent。

### 5.6 Informed Trader 怎么用?

| 选项 | 代表队 | 效果 |
|---|---|---|
| 不用 | pe049395 (P2 时还没公开) | 基线 |
| 直接 copy trade | Alpha Animals, CMU (部分产品) | 简单粗暴有效 |
| **作为 regime 调整阈值** | **Frankfurt** | 高级但有效 |
| **只用它，放弃 stat arb** | **Sylvain-Topeza (Basket 2)** | 极端版，避免复杂性 |

**推荐**: 先 copy trade 保底，再尝试 regime 调整。不要跳过 copy trade 直接做 regime (Alpha Animals 就栽在这里)。

### 5.7 两个 Basket 共享 constituent 时怎么分 position?

这是 P3 独有的问题。

| 选项 | 代表队 | 优缺点 |
|---|---|---|
| 两个 basket 各占一半 position limit | 简单队 | 利用率低 |
| 交易 premium 差 (B1 - B2) | **CMU Physics** | constituent 风险大部分抵消 |
| 按 Sharpe ratio 分配 | - | 理论最优但实现复杂 |
| **两个 basket 用不同策略** | **Sylvain-Topeza** | 简单且每个策略能吃满 |
| **100% B1 + 60% B2 premium-diff + 32% B2 z-score + 8% B2 MM** | **CMU Physics** | 利用率 100% |

**推荐**: CMU Physics 的分层方案最接近最优; 如果嫌复杂, Sylvain-Topeza 的 "不同 basket 不同策略" 最简单实用。

---

## 6. 踩过的坑 (所有人都可能再踩)

按严重程度排序:

### 🔴 严重: Position limit 未 clamp 导致订单被拒

- **Alpha Animals** 整个 Round 2 的 stat arb 因为这个 bug 没跑起来。
- **修复**: 订单 size 要先 `min(abs(intended), max_allowed_buy_volume)` 再下。Frankfurt 代码里每个 `bid()` 和 `ask()` 都有这一步。

### 🔴 严重: Jasper visualizer 占内存触发 Lambda 重启

- **CMU Physics** Round 3 从 7th 跌到 241st 的主要原因。
- **修复**: 提交前移除 visualizer (只保留自己的 log)。Frankfurt 的 logger 限制了 stdout < 3750 chars/tick。

### 🟠 中等: backtest pass，live 失败 (滑点)

- **Linear Utility** backtest 135k 实际 111k (滑点 ~18%)
- **jmerle** Round 4 coconuts 亏 77k 因为 directional prediction 不准
- **修复**: 
  - 别对 backtest 数字过度拟合
  - 参数选择时看 PnL landscape 稳定性，不看峰值
  - 加保守 buffer

### 🟠 中等: 盲目套 z-score 没看 premium 是否漂移

- 很多中游队直接 z = (spread - 0) / std, spread 均值不是 0 → 系统性偏向做空 basket
- **修复**: 第一周先做 EDA，看 spread 的 distribution，再决定 mean 是什么值。

### 🟠 中等: Round 3 Volcanic Rock mean-reversion 反而赢了

- P3 Round 3 的"真信号"其实不是期权 IV smile, 而是 Volcanic Rock underlying 的 mean reversion。Frankfurt 因为 minimum-regret 思维做了 50% 暴露，但 CMU Physics 和 Alpha Animals 一度完全错过。
- **教训**: **不要把所有鸡蛋放在自己 conviction 最高的策略上**。留 20-30% 仓位作为对冲"其他队可能找到的 alpha"。

### 🟡 小坑: 硬编码了 INITIAL_ETF_PREMIUMS

- 如果初始值偏离实际太远，running mean 要很多 tick 才能收敛 (尤其 n_hist=60k 时)
- **修复**: 每 round 用上一 round 的实际 spread 均值更新 INITIAL_ETF_PREMIUMS

### 🟡 小坑: 不理解 Conversion 语义 (P3 Round 4)

- Alpha Animals Round 4 在 Macarons 上因此亏钱
- ETF 题本身没有 conversion，但 P4 如果加了新机制，务必先把机制跑通再做策略。

### 🟡 小坑: orderbook 只有 1-2 档的极端情形

- `get_walls()` 假设至少有 2 档。当只有 1 档时 wall_bid == wall_ask，wall_mid 就是 raw mid。
- Frankfurt 代码里对此有 `try/except pass`，但完全 robust 的写法是显式判断 `if bid_wall and ask_wall` 再算 wall_mid。

---

## 7. 对 P4 Round 3 的实操建议

假设 P4 Round 3 依然是 ETF 类题（极可能），按**优先级**分阶段:

### 阶段 0: 开赛前 (现在做)

1. ✅ **准备 Wall Mid 工具函数** — 从 orderbook 提取 bid_wall, ask_wall, wall_mid。这个通用到所有产品, 不只 basket。
2. ✅ **准备 Increment Mean 工具函数** — `mean = mean + (x - mean) / n`。
3. ✅ **Fork jmerle 的 backtester** — Frankfurt 也是 fork 版。删掉 visualizer 的依赖防止 Lambda 爆内存。
4. ✅ **跑 P3 数据演练一次** — 用 P3 的 Round 2 数据把 Frankfurt 的 ETF 策略跑起来，确认能 reproduce ~50k/round，作为基线。

### 阶段 1: Day 1 EDA (不急着交易)

1. 看 spread 的 **raw distribution** (basket.mid - synthetic, 用 raw mid)
2. 看 spread 的 **Wall-Mid distribution** (会更 tight)
3. 看 spread 的 **time series** 是否均值回复 (autocorrelation of first-diff)
4. **看不同 constituent 的 return 自相关** — 确认 constituent 是 random walk (如果不是，可能有 alpha 可挖)
5. 找 **size anomaly trades** — 历史 Olivia 的特征是 size=15 在日极值。P4 的 informed trader 可能换成其他 size。

### 阶段 2: Day 1–2 基线策略

**最小可行策略** (20–40k/day 目标):
```python
1. 用 Wall Mid 算 basket & constituents 的 mid
2. spread = basket.wall_mid - sum(factor * const.wall_mid)
3. 初始 premium = 历史 spread 的均值 (上一 round 结束时估)
4. 每 tick 增量更新 premium, n 初始 50000
5. signal = spread - premium
6. 如果 signal > +THR, 做空 basket (吃深到 bid_wall)
7. 如果 signal < -THR, 做多 basket (吃深到 ask_wall)
8. 如果 |signal| < THR 但 basket.initial_position != 0, 平仓
9. THR 通过 backtest grid search
10. 先不对冲 (pe049395 和 Stanford 都没对冲)
```

### 阶段 3: Day 2–3 优化

1. **加 Olivia 识别** — 先做简单版: 追踪每个 constituent 每日的 min/max, 当在极值出现 size=15 (或其他 size) 的 trade 时打 LONG/SHORT tag
2. **把 Olivia 方向作为阈值偏移** — Frankfurt 用 ±90 的 adjustment
3. **Basket 2 (如有) 考虑完全跟 Olivia 不做 stat arb** (Sylvain-Topeza)
4. **Basket 头寸不满时做 market make** — CMU 用这个多赚 5k/day

### 阶段 4: Day 3 稳定化

1. **参数选 landscape 稳定的**, 不选 backtest 最高的
2. **减小仓位**如果领先
3. **加 fallback** — 如果关键信号失效, 自动退到 naive stat arb

### 不要做的事

- ❌ 花一天学 Ornstein-Uhlenbeck 等高级模型 (Frankfurt 明确说不需要)
- ❌ 上 ML 模型预测 constituent (Alpha Animals 试了没用)
- ❌ 100% 对冲 (Linear Utility 和 jmerle 的教训)
- ❌ 从 basket 推 constituent 反向交易 (Martin Oravec 的思路，有 EV 但 variance 太大)
- ❌ 忽略 position limit (Alpha Animals 的灾难)

---

## 8. 附录: Frankfurt Hedgehogs 核心代码解读

以下为 `FrankfurtHedgehogs_polished.py` 里 ETF 部分的简化版 + 注释。完整代码见:
https://github.com/TimoDiehm/imc-prosperity-3/blob/main/FrankfurtHedgehogs_polished.py

```python
# ========== 超参 ==========
ETF_BASKET_SYMBOLS = ['PICNIC_BASKET1', 'PICNIC_BASKET2']
ETF_CONSTITUENT_SYMBOLS = ['CROISSANTS', 'JAMS', 'DJEMBES']
ETF_CONSTITUENT_FACTORS = [[6, 3, 1], [4, 2, 0]]  # B1 / B2 的组成

BASKET_THRESHOLDS = [80, 50]              # basket 1, basket 2 各自的对称阈值
INITIAL_ETF_PREMIUMS = [5, 53]            # 初始 premium 估计 (来自历史)
n_hist_samples = 60_000                   # 增量均值的初始 n
ETF_INFORMED_CONSTITUENT = 'CROISSANTS'   # Olivia 所在的 constituent
ETF_THR_INFORMED_ADJS = [90, 90]          # Olivia 方向带来的阈值偏移
ETF_CLOSE_AT_ZERO = True                  # 穿 0 (含 informed_adj) 立即平仓
ETF_HEDGE_FACTOR = 0.5                    # JAMS/DJEMBES 对冲比例
```

### 8.1 Spread 计算 (核心)

```python
def calculate_spread(self, basket):
    b_idx = ETF_BASKET_SYMBOLS.index(basket.name)
    constituents = [self.informed_constituent] + self.hedging_constituents
    # 按 SYMBOL 顺序 sort 保证 factor 对齐
    const_prices = [c.wall_mid for c in sorted_by_constituent_order]
    
    # 合成价 = 各 constituent 的 Wall Mid 加权
    index_price = np.asarray(const_prices) @ np.asarray(ETF_CONSTITUENT_FACTORS[b_idx])
    
    # raw spread = basket 的 Wall Mid - 合成价
    etf_price = basket.wall_mid
    raw_spread = etf_price - index_price

    # Running premium (增量均值)
    old_mean, n = self.last_traderData.get(
        f'ETF_{b_idx}_P', [INITIAL_ETF_PREMIUMS[b_idx], n_hist_samples])
    n += 1
    mean_premium = old_mean + (raw_spread - old_mean) / n
    self.new_trader_data[f'ETF_{b_idx}_P'] = [mean_premium, n]
    
    # 最终信号
    spread = raw_spread - mean_premium
    return spread
```

### 8.2 Basket 开仓/平仓

```python
def get_basket_orders(self):
    for b_idx, basket in enumerate(self.baskets):
        # Olivia 方向带来的阈值偏移
        informed_thr_adj = {
            LONG: +ETF_THR_INFORMED_ADJS[b_idx],
            SHORT: -ETF_THR_INFORMED_ADJS[b_idx]
        }.get(self.informed_direction, 0)
        
        base_thr = BASKET_THRESHOLDS[b_idx]
        spread = self.spreads[b_idx]
        
        # 开空: spread 太高
        if spread > (base_thr + informed_thr_adj):
            basket.ask(basket.bid_wall, basket.max_allowed_sell_volume)
        # 开多: spread 太低
        elif spread < (-base_thr + informed_thr_adj):
            basket.bid(basket.ask_wall, basket.max_allowed_buy_volume)
        # ETF_CLOSE_AT_ZERO: 穿过 informed_thr_adj 且有仓位则平
        elif ETF_CLOSE_AT_ZERO:
            if spread > informed_thr_adj and basket.initial_position > 0:
                basket.ask(basket.bid_wall, basket.initial_position)
            elif spread < informed_thr_adj and basket.initial_position < 0:
                basket.bid(basket.ask_wall, -basket.initial_position)
```

**注意点**:
- 吃**深到 bid_wall / ask_wall** (pe049395 也是这个做法): 付 spread 换确定成交
- 开仓用 `max_allowed_*_volume`, 平仓用 `initial_position`, 不会超限
- 平仓条件是 `spread > informed_thr_adj` 而不是 `spread > 0`, 保证 Olivia 方向的仓位能继续持有更久

### 8.3 Constituent 处理

```python
def get_constituent_orders(self):
    # CROISSANTS: 纯跟 Olivia, 不做 basket 对冲
    expected = {LONG: +250, SHORT: -250}.get(self.informed_direction, 0)
    remaining = expected - self.informed_constituent.initial_position
    if remaining > 0: self.informed_constituent.bid(ask_wall, remaining)
    elif remaining < 0: self.informed_constituent.ask(bid_wall, -remaining)
    
    # JAMS, DJEMBES: 50% 对冲
    for hc in self.hedging_constituents:
        expected_hedge = 0
        for b_idx, basket in enumerate(self.baskets):
            factor = ETF_CONSTITUENT_FACTORS[b_idx][idx_of(hc)]
            # 注意符号: basket 做多 -> constituent 对应做空以对冲
            expected_hedge += -basket.expected_position * factor * ETF_HEDGE_FACTOR
        remaining = round(expected_hedge - hc.initial_position)
        # 下单到对应方向...
```

**注意点**:
- `basket.expected_position` 是"本 tick 预期成交后的 basket 仓位", 而不是 initial_position。这样如果本 tick 打算开 basket 的 +20, 对冲会用到这个 +20 算 hedge size。
- `ETF_HEDGE_FACTOR = 0.5` 是关键: 把对冲风险降一半。如果改 0 就是"不对冲", 改 1.0 就是"完全对冲"。

### 8.4 Olivia 识别

```python
def check_for_informed(self):
    # 从 traderData 读上一次检测到的时间戳
    informed_bought_ts, informed_sold_ts = self.last_traderData.get(
        self.name, [None, None])
    
    # 扫描所有成交 (market + own), 找 Olivia
    trades = (self.state.market_trades.get(self.name, [])
              + self.state.own_trades.get(self.name, []))
    for trade in trades:
        if trade.buyer == 'Olivia':   # Round 5 后 trader_id 公开
            informed_bought_ts = trade.timestamp
        if trade.seller == 'Olivia':
            informed_sold_ts = trade.timestamp
    
    # 方向判定: 最近一次操作是哪个方向
    if informed_bought_ts is None and informed_sold_ts is None:
        return NEUTRAL
    elif informed_bought_ts is not None and informed_sold_ts is not None:
        return LONG if informed_bought_ts > informed_sold_ts else SHORT
    # ... 其他 edge cases
```

**Round 5 之前** Olivia 不公开时，Frankfurt 用的是 "追踪当日 min/max + size=15 的 trade 在极值出现" 来识别。这个检测逻辑 Frankfurt 没公开 (他们说"避免盲抄"), 但原理明确: 每个 tick 更新当日 min/max, 如果当前 tick 有一个 size=15 的 trade 成交价正好在当日最值附近, 且方向和"买低卖高"一致, 就打标。

---

## 9. 延伸阅读 (原始链接)

| 资源 | 链接 |
|---|---|
| Frankfurt Hedgehogs 完整 README | https://github.com/TimoDiehm/imc-prosperity-3 |
| Frankfurt Hedgehogs 代码 | https://github.com/TimoDiehm/imc-prosperity-3/blob/main/FrankfurtHedgehogs_polished.py |
| CMU Physics (chrispyroberts) | https://github.com/chrispyroberts/imc-prosperity-3 |
| Alpha Animals (CarterT27) | https://github.com/CarterT27/imc-prosperity-3 |
| Sylvain-Topeza | https://github.com/Sylvain-Topeza/imc-prosperity-3 |
| Linear Utility (P2 #2) | https://github.com/ericcccsliu/imc-prosperity-2 |
| jmerle P2 (工具作者) | https://github.com/jmerle/imc-prosperity-2 |
| jmerle P3 backtester | https://github.com/jmerle/imc-prosperity-3-backtester |
| pe049395 | https://github.com/pe049395/IMC-Prosperity-2024 |
| Stanford Cardinal (P1) | https://github.com/ShubhamAnandJain/IMC-Prosperity-2023-Stanford-Cardinal |
| Martin Oravec Medium | https://medium.com/@oravec.martin01/imc-prosperity-3-be859180f133 |
| Prosperity 3 Wiki | https://imc-prosperity.notion.site/Prosperity-3-Wiki-19ee8453a09380529731c4e6fb697ea4 |

---

*End of Report*

