# 确定性上涨市场(INTARIAN_PEPPER_ROOT)做市策略：伪代码 v0（简化版）

---

## 0. 全局参数

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
全部为预设常数，无运行时估计
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

// 市场环境
μ          ← 价格漂移率 0.001 float
T          ← 交易终止时刻 100_000 int
N          ← 最大持仓量 80 int
ε          ← 最小 tick 1 int
σ          ← 可捕获价差 
S_max      ← 单笔最大成交量（截断上限）
fair_price ← 合理价格，用于判断非理性挂单 int(μ * t + 12_000) 

// 被动 fill：按成交量分档的到达率
// λ_b[s] = 每单位时间，恰好成交 s 单位的 bid fill 到达几次
λ_b[1], λ_b[2], ..., λ_b[S_max]    ← 各档预设常数, 暂时用合理数填补
λ_a[1], λ_a[2], ..., λ_a[S_max]    ← 各档预设常数

// 非理性单：按量分档的到达率，越价幅度取固定均值
// γ⁺[v] = 每单位时间，量为 v 的非理性卖单到达几次
γ⁺[1], γ⁺[2], ..., γ⁺[S_max]      ← 各档预设常数
γ⁻[1], γ⁻[2], ..., γ⁻[S_max]      ← 各档预设常数
ξ̄          ← 越价幅度均值（常数）

// 导出量（仅做参考，不参与计算）
// λ_b_total = Σ_s λ_b[s]         总笔数率
// λ_b_vol   = Σ_s s · λ_b[s]     总量率
// s̄_b       = λ_b_vol / λ_b_total 平均单笔量

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
状态变量
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
t          ← 当前时刻
q          ← 当前持仓，q ∈ {0, 1, ..., N}
V[i][q]    ← 值函数表
ΔV[i][q]   ← V[i][q] - V[i][q-1]
```

---

## 1. 值函数预计算（启动时一次性算完）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOLVE_VALUE_FUNCTION():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    dt ← T / M
    V[M][q] ← 0  for all q

    for i from M-1 down to 0:
        for q from 0 to N:

            // ━━━ ① 持仓漂移 ━━━
            drift ← q · μ · dt

            // ━━━ ② 被动 bid fill（买入 s 单位，q → q+s）━━━
            bid_term ← 0
            if q < N:
                for s from 1 to min(S_max, N - q):
                    value_of_fill ← V[i+1][q + s] - V[i+1][q] + s · σ/2
                    bid_term += λ_b[s] · max(value_of_fill, 0) · dt

                // s 超过剩余仓位：实际只能吃 N-q
                for s from N - q + 1 to S_max:
                    cap ← N - q
                    value_of_fill ← V[i+1][N] - V[i+1][q] + cap · σ/2
                    bid_term += λ_b[s] · max(value_of_fill, 0) · dt

            // ━━━ ③ 被动 ask fill（卖出 s 单位，q → q-s）━━━
            ask_term ← 0
            if q > 0:
                for s from 1 to min(S_max, q):
                    value_of_fill ← V[i+1][q - s] - V[i+1][q] + s · σ/2
                    ask_term += λ_a[s] · max(value_of_fill, 0) · dt

                // s 超过当前仓位：实际只能卖 q
                for s from q + 1 to S_max:
                    value_of_fill ← V[i+1][0] - V[i+1][q] + q · σ/2
                    ask_term += λ_a[s] · max(value_of_fill, 0) · dt

            // ━━━ ④ 非理性卖单（主动买入，量为 v，利润 ξ̄/单位）━━━
            take_buy ← 0
            if q < N:
                for v from 1 to S_max:
                    cap ← min(v, N - q)
                    // 逐单位贪心确定最优 take 量
                    best_j ← 0
                    total_value ← 0
                    for j from 1 to cap:
                        marginal ← ξ̄ + V[i+1][q + j] - V[i+1][q + j - 1]
                        if marginal > 0:
                            best_j ← j
                            total_value += marginal
                        else:
                            break
                    take_buy += γ⁺[v] · total_value · dt

            // ━━━ ⑤ 非理性买单（主动卖出，ξ̄ vs 持有价值）━━━
            take_sell ← 0
            if q > 0:
                for v from 1 to S_max:
                    cap ← min(v, q)
                    surplus ← 0
                    for j from 1 to cap:
                        marginal_hold ← V[i+1][q - j + 1] - V[i+1][q - j]
                        if ξ̄ > marginal_hold:
                            surplus += ξ̄ - marginal_hold
                        else:
                            break
                    take_sell += γ⁻[v] · surplus · dt

            // ━━━ 合成 ━━━
            V[i][q] ← drift + bid_term + ask_term + take_buy + take_sell + V[i+1][q]

    // 边际持有价值
    for i from 0 to M:
        ΔV[i][0] ← +∞
        for q from 1 to N:
            ΔV[i][q] ← V[i][q] - V[i][q-1]

    return V, ΔV
```

---

## 2. 主循环

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    V, ΔV ← SOLVE_VALUE_FUNCTION()
    q ← 0
    pnl ← 0

    while t < T:

        p_fair ← p_0 + μ · t
        best_bid_opp, best_ask_opp ← READ_OPPONENT_QUOTES()
        i ← floor(t / dt)

        MANAGE_PASSIVE_ORDERS(i, q, best_bid_opp, best_ask_opp)

        irr_sells, irr_buys ← SCAN_ORDERBOOK(p_fair)
        for order in irr_sells:
            HANDLE_IRRATIONAL_SELL(order, i, q)
        for order in irr_buys:
            HANDLE_IRRATIONAL_BUY(order, i, q)

        for fill in POLL_FILLS():
            PROCESS_FILL(fill)

        t ← now()
```

---

## 3. 被动挂单

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANAGE_PASSIVE_ORDERS(i, q, best_bid_opp, best_ask_opp):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    // ── bid 侧 ──
    if q < N:
        E_bid ← 0
        for s from 1 to min(S_max, N - q):
            E_bid += λ_b[s] · (V[i][q + s] - V[i][q] + s · σ/2)
        for s from N - q + 1 to S_max:
            cap ← N - q
            E_bid += λ_b[s] · (V[i][N] - V[i][q] + cap · σ/2)

        if E_bid > 0:
            PLACE_OR_AMEND(BID, price = best_bid_opp + ε, size = N - q)
        else:
            CANCEL(BID)
    else:
        CANCEL(BID)

    // ── ask 侧 ──
    if q > 0:
        E_ask ← 0
        for s from 1 to min(S_max, q):
            E_ask += λ_a[s] · (V[i][q - s] - V[i][q] + s · σ/2)
        for s from q + 1 to S_max:
            E_ask += λ_a[s] · (V[i][0] - V[i][q] + q · σ/2)

        if E_ask > 0:
            PLACE_OR_AMEND(ASK, price = best_ask_opp - ε, size = q)
        else:
            CANCEL(ASK)
    else:
        CANCEL(ASK)
```

---

## 4. 非理性订单处理

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HANDLE_IRRATIONAL_SELL(order, i, q):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ξ ← p_fair - order.price
    capacity ← N - q

    if capacity == 0:
        return

    take_size ← 0
    for j from 1 to min(order.size, capacity):
        if ξ + V[i][q + j] - V[i][q + j - 1] > 0:
            take_size ← j
        else:
            break

    if take_size > 0:
        SEND_MARKET_ORDER(BUY, size = take_size, price = order.price)
        q ← q + take_size
        pnl -= take_size · order.price


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HANDLE_IRRATIONAL_BUY(order, i, q):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ξ ← order.price - p_fair

    if q == 0:
        return

    take_size ← 0
    for j from 1 to min(order.size, q):
        if ξ > ΔV[i][q - j + 1]:
            take_size ← j
        else:
            break

    if take_size > 0:
        SEND_MARKET_ORDER(SELL, size = take_size, price = order.price)
        q ← q - take_size
        pnl += take_size · order.price
```

---

## 5. 被动成交 & 终止

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROCESS_FILL(fill):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    if fill.side == BID:
        q ← min(q + fill.size, N)
        pnl -= fill.size · fill.price

    if fill.side == ASK:
        q ← max(q - fill.size, 0)
        pnl += fill.size · fill.price

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ON_APPROACHING_TERMINAL(t, q):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    if T - t < EMERGENCY_WINDOW and q > 0:
        SEND_MARKET_ORDER(SELL, size = q, price = BEST_BID)
        pnl += q · BEST_BID
        q ← 0
```

---

## 6. 参数配置示例

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
假设 S_max = 5，参数配置表：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

被动 bid fill 到达率（笔/秒）：
┌─────┬────────┬────────┬────────┬────────┬────────┐
│  s  │   1    │   2    │   3    │   4    │   5    │
├─────┼────────┼────────┼────────┼────────┼────────┤
│λ_b  │  0.50  │  0.20  │  0.08  │  0.03  │  0.01  │
└─────┴────────┴────────┴────────┴────────┴────────┘
→ 总笔数率 0.82/s，平均单笔量 1.50，总量率 1.23/s

被动 ask fill 到达率（笔/秒）：
┌─────┬────────┬────────┬────────┬────────┬────────┐
│  s  │   1    │   2    │   3    │   4    │   5    │
├─────┼────────┼────────┼────────┼────────┼────────┤
│λ_a  │  0.40  │  0.15  │  0.06  │  0.02  │  0.01  │
└─────┴────────┴────────┴────────┴────────┴────────┘

非理性卖单到达率（笔/秒）：
┌─────┬────────┬────────┬────────┬────────┬────────┐
│  v  │   1    │   2    │   3    │   4    │   5    │
├─────┼────────┼────────┼────────┼────────┼────────┤
│ γ⁺  │  0.05  │  0.02  │  0.005 │  0.001 │ 0.0005 │
└─────┴────────┴────────┴────────┴────────┴────────┘

非理性买单到达率（笔/秒）：
┌─────┬────────┬────────┬────────┬────────┬────────┐
│  v  │   1    │   2    │   3    │   4    │   5    │
├─────┼────────┼────────┼────────┼────────┼────────┤
│ γ⁻  │  0.03  │  0.01  │  0.003 │  0.001 │ 0.0003 │
└─────┴────────┴────────┴────────┴────────┴────────┘

越价幅度：ξ̄ = 2 ticks

直觉：大单稀少，小单频繁。
到达率的递减速度体现市场微观结构特征。
```

