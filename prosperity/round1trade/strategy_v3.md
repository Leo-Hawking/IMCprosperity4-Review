# 实战策略 v3

---

## 0. 参数

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
常量
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
P_SETTLE    = 12100
N           = 80
ε           = 1 tick

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
可调参数
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUILD_PREMIUM       = 7       // Phase 1 建仓溢价
ASK_SPREAD_MIN      = 0       // Phase 2 ask 最低高于 fair price 的距离
ASK_SIZE            = 8       // Phase 2 ask 挂单量
IRR_TAKE_THRESHOLD  = 8       // Phase 2 非理性买单：对手量 < 此值才吃
BID_PREMIUM         = 0     // Phase 2 bid 最高低于 fair price 的距离

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
状态
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
t               当前时刻
q               当前持仓
phase ∈ {1, 2}  当前阶段
last_bid_price  bid 真空回退锚点
last_ask_price  ask 真空回退锚点
```

---

## 1. 主循环

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    q ← 0
    phase ← 1

    while t < T:

        p_fair ← p_0 + μ · t

        // ━━━ 全局优先：结算价套利 ━━━
        // 优先级最高，任何阶段都执行
        GLOBAL_SETTLE_CHECK(p_fair)

        // ━━━ 阶段转换 ━━━
        if phase == 1 and q >= N:
            phase ← 2

        // ━━━ 执行当前阶段 ━━━
        if phase == 1:
            PHASE_1(p_fair)
        elif phase == 2:
            PHASE_2(p_fair)

        // ━━━ 处理被动成交 ━━━
        for fill in POLL_FILLS():
            if fill.side == BID:
                q ← min(q + fill.size, N)
            if fill.side == ASK:
                q ← max(q - fill.size, 0)

        t ← now()

    // 结算
    pnl += q · P_SETTLE
```

---

## 2. 全局优先逻辑：结算价套利

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GLOBAL_SETTLE_CHECK(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // 如果有人挂了 bid ≥ P_SETTLE，立刻全卖
    // 这是无风险利润：卖价 ≥ 结算价，卖完后结算时仓位 0 也不亏

    for bid in orderbook.bids sorted by price descending:
        if bid.price >= P_SETTLE and q > 0:
            take_size ← min(bid.size, q)
            SEND_MARKET_ORDER(SELL, size=take_size, price=bid.price)
            q -= take_size
        else:
            break

    // 执行完后，如果原来有 ask 挂单可能需要调整
    // 但因为此逻辑优先级最高，后续阶段逻辑会自动修正
```

---

## 3. Phase 1：建仓

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE_1(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // 吃掉所有 ask ≤ fair + 7，直到满仓
    take_ceiling ← p_fair + BUILD_PREMIUM

    for ask in orderbook.asks sorted by price ascending:
        if ask.price > take_ceiling or q >= N:
            break
        take_size ← min(ask.size, N - q)
        SEND_MARKET_ORDER(BUY, size=take_size, price=ask.price)
        q += take_size

    // 被动 bid 补仓
    if q < N:
        bid_price ← GET_MY_BID_PRICE()
        PLACE_OR_AMEND(BID, price=bid_price, size=N - q)
    else:
        CANCEL(BID)

    // 不挂 ask
    CANCEL(ASK)
```

---

## 4. Phase 2：满仓做市

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE_2(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // ━━━ A: 主动吃非理性单 ━━━
    TAKE_IRRATIONAL(p_fair)

    // ━━━ B: 被动挂单 ━━━
    if q == N:
        // 满仓状态：挂 ask，不挂 bid
        MANAGE_ASK(p_fair)
        CANCEL(BID)
    else:
        // 仓位不满：挂 bid 补仓，不挂 ask
        MANAGE_BID(p_fair)
        CANCEL(ASK)
```

### 4A. 主动吃非理性单

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TAKE_IRRATIONAL(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // ── 买入侧：吃掉所有低于 fair price 的 ask，直到满仓 ──
    if q < N:
        for ask in orderbook.asks sorted by price ascending:
            if ask.price >= p_fair or q >= N:
                break
            take_size ← min(ask.size, N - q)
            SEND_MARKET_ORDER(BUY, size=take_size, price=ask.price)
            q += take_size

    // ── 卖出侧：吃高于 fair price 的 bid，但只在满仓时且对手量 < 8 ──
    if q == N:
        for bid in orderbook.bids sorted by price descending:
            if bid.price <= p_fair:
                break
            if bid.size < IRR_TAKE_THRESHOLD:
                take_size ← bid.size       // 全吃
                SEND_MARKET_ORDER(SELL, size=take_size, price=bid.price)
                q -= take_size
            // else: 量 ≥ 8，skip
```

### 4B. Ask 挂单（满仓时）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANAGE_ASK(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ask_price ← GET_MY_ASK_PRICE()
    ask_floor ← p_fair + ASK_SPREAD_MIN

    // 确保不低于 fair + ASK_SPREAD_MIN
    if ask_price < ask_floor:
        ask_price ← ask_floor

    PLACE_OR_AMEND(ASK, price=ask_price, size=ASK_SIZE)
```

### 4C. Bid 挂单（非满仓时）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANAGE_BID(p_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    bid_price ← GET_MY_BID_PRICE()
    bid_ceiling ← p_fair + BID_PREMIUM

    // 确保不高于 fair + BID_PREMIUM
    if bid_price > bid_ceiling:
        bid_price ← bid_ceiling

    PLACE_OR_AMEND(BID, price=bid_price, size=N - q)
```

---

## 5. 报价定位

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET_MY_BID_PRICE():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    best_bid_opp ← 排除自己后的最优 bid
    if exists:
        price ← best_bid_opp + ε
        last_bid_price ← price
    else:
        price ← last_bid_price
    return price

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET_MY_ASK_PRICE():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    best_ask_opp ← 排除自己后的最优 ask
    if exists:
        price ← best_ask_opp - ε
        last_ask_price ← price
    else:
        price ← last_ask_price
    return price
```

---

## 6. 状态机

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│     ╔══════════════════════════════════════════╗          │
│     ║  GLOBAL: bid ≥ 12100 → 立刻全卖         ║          │
│     ║  优先级最高，随时触发                     ║          │
│     ╚══════════════════════════════════════════╝          │
│                                                          │
│     ┌──────────────┐    q = N    ┌──────────────┐        │
│     │  Phase 1     │ ──────────→ │  Phase 2     │        │
│     │  建仓        │             │  做市        │        │
│     │              │             │              │        │
│     │ • 吃 ask ≤   │             │ 满仓时:      │        │
│     │   fair + 7   │             │ • ask: 8单位 │        │
│     │ • bid: N-q   │             │ • 吃非理性bid│        │
│     │ • ask: 关闭  │             │   (量<8才吃) │        │
│     └──────────────┘             │              │        │
│                                  │ 非满仓时:    │        │
│                                  │ • bid: N-q   │        │
│                                  │ • 吃非理性ask│        │
│                                  │ • ask: 关闭  │        │
│                                  └──────────────┘        │
│                                                          │
│  Phase 2 内部状态切换：                                   │
│                                                          │
│     q = N ←──── bid fill / take ask ────→ q < N         │
│    [挂ask]      ←── ask fill / take bid ──→  [挂bid]     │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 7. 参数讨论

```
┌──────────────────┬───────┬────────────────────────────────────┐
│ 参数              │ 当前值│ 备注                               │
├──────────────────┼───────┼────────────────────────────────────┤
│ BUILD_PREMIUM    │ 7     │ 开局吃单激进度                     │
│                  │       │ 越大建仓越快但单位成本越高         │
│                  │       │ 上限：P_SETTLE - p_0               │
├──────────────────┼───────┼────────────────────────────────────┤
│ ASK_SPREAD_MIN   │ 0     │ 卖单最低距 fair price 的距离       │
│                  │       │ = 0 表示只要高于 fair 就愿意卖     │
│                  │       │ 调高 → 做市频率降低但单笔利润更高  │
│                  │       │ 调低 → 做市频率升高但可能亏漂移    │
├──────────────────┼───────┼────────────────────────────────────┤
│ ASK_SIZE         │ 8     │ 单次最大卖出暴露                   │
│                  │       │ 太大 → 一笔 fill 损失太多漂移      │
│                  │       │ 太小 → 做市收益太低                │
│                  │       │ 约束：被 fill 后要能靠 bid 买回来  │
├──────────────────┼───────┼────────────────────────────────────┤
│ IRR_TAKE_THRESH  │ 8     │ 非理性买单吃单门槛                 │
│                  │       │ < 8 才吃全部，≥ 8 不吃             │
│                  │       │ 理由：大单可能导致仓位缺口太大     │
│                  │       │ 后续优化方向：改为「吃 min(量, 8)」│
├──────────────────┼───────┼────────────────────────────────────┤
│ BID_PREMIUM      │ 待定  │ 非满仓时 bid 最高溢价              │
│                  │       │ 正值 = 愿意高于 fair 买入           │
│                  │       │ 理由同建仓：结算价 > 当前价         │
│                  │       │ 建议初始值 = BUILD_PREMIUM = 7     │
│                  │       │ 或更保守 = 3（因为已过建仓期）     │
└──────────────────┴───────┴────────────────────────────────────┘
```
