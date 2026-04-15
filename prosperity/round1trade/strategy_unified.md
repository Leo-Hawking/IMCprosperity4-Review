# 确定性上涨 + 终点结算做市策略：统一框架

---

## 0. 模型设定

```
市场：
  p(t) = p_0 + μ·t，μ > 0 已知，t ∈ [0, T]
  终点结算价 P_SETTLE = 12100，持仓按此价结算

订单簿：
  屏蔽模型：最优价完全遮挡后方订单
  优先级：同价位对手 bot 先成交
  ⟹ 唯一可行报价 = 对手最优价 ± ε

持仓：
  q ∈ {0, 1, ..., N}，N = 80
```

---

## 1. 参数表

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
已知常量
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
P_SETTLE        12100       终点结算价
μ               从数据算     线性漂移率
T               已知         终止时刻
N               80           最大仓位
ε               已知         最小 tick

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
需标定参数
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
λ_b_vol         bid 侧被动成交量率（量/单位时间）
λ_a_vol         ask 侧被动成交量率
δ_b             bid 侧典型半价差（fair - my_bid）
δ_a             ask 侧典型半价差（my_ask - fair）
γ⁺              非理性卖单到达率
γ⁻              非理性买单到达率
ξ̄               越价幅度均值

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
导出参数
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
s_a_max = floor(2 · δ_a · λ_b_vol / μ)
        ask 挂单量上限
        来源：单次做市周转中「价差收益 > 空仓漂移损失」的约束

k       = max(2 · s_a_max, 1)
        做市缓冲仓位数

q_target = N - k
        Phase 2 目标仓位（建仓完成线 & 做市基准线）

q_floor  = N - 2·k
        Phase 2 仓位绝对下限（防止过度卖出）
```

---

## 2. 收益来源分解

```
Phase 2 的做市+吃单策略有五个独立收益流：

①  持仓漂移          q · μ · dt
    ── 仓位越高越赚，贯穿全程

②  被动 bid fill      成交时赚 δ_b，且买入的仓位继续吃漂移
    ── 纯正收益，越多越好

③  被动 ask fill      成交时赚 δ_a，但卖出的仓位停止吃漂移
    ── 净收益 = δ_a - μ·E[买回时间]·(卖出量/2)
    ── 仅当 s_a_max ≥ 1 时为正

④  主动 take 低价 ask  买入价 p，结算收 P_SETTLE
    ── 确定利润 P_SETTLE - p，只要 p < P_SETTLE - C_slot

⑤  主动 take 高价 bid  卖出赚即时溢价 ξ，但放弃漂移和结算收益
    ── 净收益 = ξ - (P_SETTLE - p_fair) + C_slot
    ── 仅当溢价极大时为正

为什么 MM + take > 纯持仓：
  纯持仓仅有收益流 ①
  MM + take 额外获得 ②③④⑤
  ② 恒正
  ④ 在稀疏市场中是主要增量来源（主动获取低于结算价的仓位）
  ③ 只要 s_a_max 标定正确就是正贡献
  ⑤ 是小概率高收益的期权
```

---

## 3. 状态变量与报价

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
状态
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
t                   当前时刻
q                   当前持仓
phase ∈ {1, 2, 3}   当前阶段
pnl                 已实现盈亏
last_bid_price      上一时刻 bid 报价（真空回退用）
last_ask_price      上一时刻 ask 报价（真空回退用）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET_MY_BID_PRICE():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    best_bid_opp ← 订单簿上排除自己后的最优 bid
    if best_bid_opp exists:
        price ← best_bid_opp + ε
        last_bid_price ← price
    else:
        price ← last_bid_price
    return price

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET_MY_ASK_PRICE():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    best_ask_opp ← 订单簿上排除自己后的最优 ask
    if best_ask_opp exists:
        price ← best_ask_opp - ε
        last_ask_price ← price
    else:
        price ← last_ask_price
    return price
```

---

## 4. 阶段转换

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UPDATE_PHASE():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    p_fair ← p_0 + μ · t
    remaining_drift ← P_SETTLE - p_fair

    if phase == 1:
        if q >= q_target:
            phase ← 2

    if phase == 2:
        if remaining_drift < μ / λ_b_vol:
            phase ← 3

    // 注：phase 只能 1→2→3，不回退
```

```
阶段转换条件说明：

Phase 1 → 2：q ≥ q_target = N - k
  含义：仓位已积累到足以支撑做市的水平。
  此后 ask 侧开始挂单，进入双边做市。

Phase 2 → 3：P_SETTLE - p_fair(t) < μ / λ_b_vol
  含义：剩余漂移空间已不够完成一次做市周转。
  μ/λ_b_vol 是买回一单位的期望漂移损失。
  当剩余漂移 < 此值，做市的卖出端必亏，应停止。
```

---

## 5. Phase 1：建仓

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE_1():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

目标：尽快将 q 从 0 拉到 q_target。
方式：主动吃单 + 被动 bid，不挂 ask。

    p_fair ← p_0 + μ · t
    take_ceiling ← p_fair + BUILD_PREMIUM     // 建仓溢价上限

    // ── 主动吃 ask：扫描所有低于阈值的卖单 ──
    for ask in orderbook.asks sorted by price ascending:
        if ask.price > take_ceiling or q >= N:
            break
        take_size ← min(ask.size, N - q)
        SEND_MARKET_ORDER(BUY, size=take_size, price=ask.price)
        q += take_size
        pnl -= take_size · ask.price

    // ── 被动 bid：最优价，满额 ──
    if q < N:
        PLACE_OR_AMEND(BID, price=GET_MY_BID_PRICE(), size=N - q)
    else:
        CANCEL(BID)

    // ── 不挂 ask ──
    CANCEL(ASK)
```

```
BUILD_PREMIUM 说明：
  容忍的溢价 = 你确信未来漂移能覆盖的部分。
  初始值 9 ticks（保守），可以设为 P_SETTLE - p_fair(t) 的某个比例。
  建仓期间不挂 ask 的理由：仓位宝贵，每一单位都应持有吃漂移，
  不应在积累阶段就开始卖出。
```

---

## 6. Phase 2：双边做市 + 主动吃单 + 仓位管理

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE_2():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

目标：维持高仓位吃漂移的同时，通过双边做市和主动吃单获取额外收益。
核心约束：仓位在 [q_floor, N] 区间内波动，围绕 q_target 运行。

    p_fair ← p_0 + μ · t

    // ━━━ A: 主动 take 决策 ━━━
    ACTIVE_TAKE(p_fair)

    // ━━━ B: 被动挂单管理 ━━━
    PASSIVE_ORDERS(p_fair)
```

### 6A. 主动 take

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTIVE_TAKE(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    C_slot ← COMPUTE_C_SLOT(t, q)

    // ── 买入侧：take ask 单 ──
    //
    //  三层阈值，从最激进到最保守：
    //    ask.price < p_fair                → 非理性，无条件 take
    //    ask.price < p_fair + BUILD_PREMIUM → 建仓延续，积极 take
    //    ask.price < P_SETTLE - C_slot      → 结算有利润且超过机会成本
    //
    //  统一为单一阈值：
    //    take_ceiling = max(p_fair + BUILD_PREMIUM, P_SETTLE - C_slot)
    //
    //  量限制：take 到 N 为止（满仓也是正收益，结算保底）

    take_ceiling ← max(p_fair + BUILD_PREMIUM, P_SETTLE - C_slot)

    for ask in orderbook.asks sorted by price ascending:
        if ask.price >= take_ceiling or q >= N:
            break
        take_size ← min(ask.size, N - q)
        SEND_MARKET_ORDER(BUY, size=take_size, price=ask.price)
        q += take_size
        pnl -= take_size · ask.price

    // ── 卖出侧：take 非理性 bid 单 ──
    //
    //  卖出 1 单位放弃的价值 = P_SETTLE - p_fair（结算利润）
    //                         + 剩余做市期权价值
    //  简化：卖出阈值 = 越价幅度 > 漂移买回成本 + 结算溢价
    //
    //  sell_threshold = (P_SETTLE - p_fair) 是绝对下限
    //    因为卖出后即使立刻按 p_fair 买回，也损失了结算利润
    //    但做市周转不是按 p_fair 买回，而是按 bid 价买回，
    //    所以实际阈值还要加上买回的漂移等待成本
    //
    //  完整阈值：
    //    sell_threshold = μ / λ_b_vol          漂移等待成本
    //                   + (P_SETTLE - p_fair)  不卖直接结算的确定利润
    //                   - δ_b                  买回时赚的半价差（部分抵消）
    //
    //  注意：这个阈值通常非常高，大多数时候不 take
    //  只有极端非理性的高价 bid 才值得卖

    sell_threshold ← μ / λ_b_vol + (P_SETTLE - p_fair) - δ_b

    for bid in orderbook.bids sorted by price descending:
        ξ ← bid.price - p_fair
        if ξ <= sell_threshold or q <= q_floor:
            break
        take_size ← min(bid.size, q - q_floor)
        SEND_MARKET_ORDER(SELL, size=take_size, price=bid.price)
        q -= take_size
        pnl += take_size · bid.price
```

```
关于卖出阈值的直觉：

  当前持有 1 单位，p_fair = 12050，P_SETTLE = 12100。
  不卖 → 结算赚 50。
  有人出价 12060（ξ = 10）→ 卖了只赚 10，放弃结算的 50，亏。
  有人出价 12180（ξ = 130）→ 卖了赚 130 > 50 + 漂移成本，值得。

  结论：在终点结算规则下，卖出阈值远高于非结算场景。
  做市的 ask fill 也受此约束——这就是为什么 s_a_max 可能很小。
```

### 6B. 被动挂单

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PASSIVE_ORDERS(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // ── bid 侧：始终挂，满额 ──
    //
    //  每一笔 bid fill 都有正收益：
    //    即时：赚 δ_b
    //    未来：新仓位吃漂移 + 结算收益
    //  无条件挂满

    if q < N:
        PLACE_OR_AMEND(BID, price=GET_MY_BID_PRICE(), size=N - q)
    else:
        CANCEL(BID)

    // ── ask 侧：有条件挂，量受控 ──
    //
    //  被动 ask fill 的盈亏：
    //    赚：δ_a（即时价差）
    //    亏：μ · s / (2 · λ_b_vol)（买回期间漂移损失，s 为卖出量）
    //        + 若未能买回，放弃 (P_SETTLE - p_fair) 的结算利润
    //
    //  在终点结算规则下，被动 ask fill 的真实成本更高：
    //    不仅是漂移损失，还有结算利润的风险
    //    因此 s_a_max 的计算应考虑结算：
    //
    //    保守版 s_a_max = floor(2 · δ_a · λ_b_vol / μ)
    //    仅当 δ_a > (P_SETTLE - p_fair) · ε' 时才挂 ask
    //    其中 ε' 是"买不回来"的概率的近似
    //
    //  简化处理：只在仓位 > q_target 时挂 ask
    //    q ≤ q_target → 不挂（仓位不富余，持有更优）
    //    q > q_target → 挂出超出部分，量 = min(q - q_target, s_a_max)
    //
    //  这确保 ask fill 只消耗"缓冲仓位"，不侵蚀核心持仓

    if q > q_target and s_a_max >= 1:
        ask_size ← min(q - q_target, s_a_max)
        PLACE_OR_AMEND(ASK, price=GET_MY_ASK_PRICE(), size=ask_size)
    else:
        CANCEL(ASK)
```

```
仓位管理总结：

  q 的运行区间：[q_floor, N]
  q 的均衡点：q_target = N - k
  
  高于 q_target：挂 ask，允许卖出，回落到 q_target
  等于 q_target：ask size = 0，仅靠 bid 和主动 take 运行
  低于 q_target：不挂 ask，积极买入（bid + 主动 take），回升到 q_target
  等于 q_floor：停止一切卖出（包括 take 非理性买单）

  q
  N ┤━━━━━━━ 硬上限
    │ · · · ·  ← 主动 take + bid fill 可能推到这里
  N-k ┤─ ─ ─ ─ q_target（均衡线）
    │           ↑ bid fill 推高    ↓ ask fill 拉低
  N-2k ┤─ ─ ─ ─ q_floor（绝对下限）
    │
  0 ┤

  锯齿在 [q_target, N] 之间波动：
    上升沿 = bid fill + 主动 take ask
    下降沿 = ask fill + 主动 take 非理性 bid（极稀少）
```

### 6C. C_slot 计算

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTE_C_SLOT(t, q):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    remaining ← T - t

    // 一个空仓位在 remaining 时间内能产生的期望额外收益

    // 来源 1：做市周转利润
    if s_a_max >= 1 and q > q_target:
        cycle_time ← 1/λ_a_vol + 1/λ_b_vol           // 一次周转耗时
        cycle_profit ← (δ_a + δ_b) - μ · 1/λ_b_vol   // 一次周转净利
        n_cycles ← remaining / cycle_time              // 能做几轮
        mm_value ← max(n_cycles · cycle_profit, 0)
    else:
        mm_value ← 0

    // 来源 2：等到更便宜单子的期望节省
    wait_value ← γ⁺ · ξ̄ · remaining

    C_slot ← max(mm_value, wait_value)

    return C_slot

    // 注意：稀疏市场中 C_slot ≈ 0，主动 take 阈值 ≈ P_SETTLE
```

---

## 7. Phase 3：终局

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE_3():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

目标：以一切低于 P_SETTLE 的价格买满仓位，等结算。
做市停止：剩余漂移不够一次周转，卖出必亏。

    // ── 主动吃一切 ask < P_SETTLE ──
    for ask in orderbook.asks sorted by price ascending:
        if ask.price >= P_SETTLE or q >= N:
            break
        take_size ← min(ask.size, N - q)
        SEND_MARKET_ORDER(BUY, size=take_size, price=ask.price)
        q += take_size
        pnl -= take_size · ask.price

    // ── 被动 bid 补满 ──
    if q < N:
        bid_price ← min(GET_MY_BID_PRICE(), P_SETTLE - ε)
        PLACE_OR_AMEND(BID, price=bid_price, size=N - q)
    else:
        CANCEL(BID)

    // ── 不挂 ask ──
    CANCEL(ASK)
```

---

## 8. 主循环

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    q ← 0
    pnl ← 0
    phase ← 1
    last_bid_price ← p_0 - δ_b
    last_ask_price ← p_0 + δ_a

    while t < T:

        UPDATE_PHASE()

        if phase == 1:    PHASE_1()
        elif phase == 2:  PHASE_2()
        elif phase == 3:  PHASE_3()

        for fill in POLL_FILLS():
            if fill.side == BID:
                q ← min(q + fill.size, N)
                pnl -= fill.size · fill.price
            if fill.side == ASK:
                q ← max(q - fill.size, 0)
                pnl += fill.size · fill.price

        t ← now()

    // ── 终点结算 ──
    pnl += q · P_SETTLE
```

---

## 9. 三阶段统一视角

```
三个阶段是同一个决策框架在不同 (t, q) 状态下的自然表现：

统一决策：
  bid:  始终挂，size = N - q
  ask:  size = min(max(q - q_target, 0), s_a_max)
  take ask: price < min(p_fair + BUILD_PREMIUM, P_SETTLE - C_slot)
  take bid: ξ > μ/λ_b_vol + (P_SETTLE - p_fair) - δ_b 且 q > q_floor

Phase 1 是这个框架在 q ≪ q_target 时的表现：
  ask size = 0（q < q_target）
  take ask 非常激进（C_slot 小 + BUILD_PREMIUM 正）
  ⟹ 表现为纯买入

Phase 2 是 q ≈ q_target 时的表现：
  ask size > 0（q 波动超过 q_target 时）
  take ask 仍然积极但受 C_slot 约束
  take bid 偶发
  ⟹ 表现为不对称双边做市 + 选择性吃单

Phase 3 是 remaining_drift → 0 时的表现：
  C_slot → 0（做市无利可图，等待无意义）
  take ask 阈值 → P_SETTLE
  ask size → 0（不值得卖）
  ⟹ 表现为纯买入到满仓

┌─────────────────────────────────────────────────────────┐
│                                                         │
│  q                                                      │
│  N ┤          ╱╲  ╱╲╱╲  ╱╲╱╲╱╲╱╲╱╲╱╲    ╱╲╱╲╱╲       │
│    │        ╱    ╱╲    ╲╱              ╲╱╲       ╲╱╲    │
│q_t ┤┈┈┈┈┈╱┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╲   │
│    │    ╱                                            ╲  │
│q_f ┤┈┈╱┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╲ │
│    │  ╱                                                 │
│  0 ┤╱                                                   │
│    └───────┼────────────────────────────┼───────────┤   │
│         Phase 1       Phase 2            Phase 3        │
│         建仓          做市+吃单          终局            │
│                                                         │
│  ask   关闭        q-q_target（受控）    关闭            │
│  bid   N-q             N-q               N-q            │
│  take  激进            选择性             无条件          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 10. 边界情况

```
1. Phase 1 建仓极慢（订单流稀疏，长时间 q < q_target）：
   不阻塞。bid 在持续工作，已有仓位在赚漂移。
   如果到 Phase 3 触发条件时仍未达 q_target，直接跳到 Phase 3。
   phase 转换：if phase == 1 and remaining_drift < μ/λ_b_vol:
                  phase ← 3   // 跳过 Phase 2

2. s_a_max = 0（做市不可行）：
   Phase 2 退化为：bid 挂满 + 主动 take ask + 持仓吃漂移。
   不挂 ask，不 take 非理性 bid。
   仍优于纯持仓（收益流 ② 和 ④ 仍然存在）。

3. 满仓后出现极便宜 ask：
   无法 take（q = N）。
   可选优化：如果同时 bid 侧有高于 sell_threshold 的非理性单，
   先卖再买。当前版本不实现，因为两个条件同时出现概率极低。

4. 订单簿单侧真空：
   用 last_bid_price / last_ask_price 回退。
   如果两侧都真空，维持现有挂单不动。
```
