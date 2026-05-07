# PEBBLES 做市与对冲算法规范

## 1. 数学模型

### 1.1 风险向量

持仓 (a, b, c, d, e) 对应 (XS, S, M, L, XL)，每分量 ∈ [-10, 10]。

由恒等式 ΣP_i ≈ 50000，组合 P&L 微分为：

$$dV = (a-e)dP_{XS} + (b-e)dP_S + (c-e)dP_M + (d-e)dP_L$$

定义风险向量：

$$R = (a-e,\ b-e,\ c-e,\ d-e)$$

风险最小化等价于最小化 ||R||²。

### 1.2 已定义变量

`fair_i`：每条腿的 fair price，由上游模块提供。

---

## 2. 做市模块（quote）

### 2.1 规则

每条腿 i：

- bid_price = best_bid_i + 1
- ask_price = best_ask_i - 1
- bid_size = 10 - q_i
- ask_size = 10 + q_i

**过滤**：

- bid_price ≥ fair_i → bid_size = 0
- ask_price ≤ fair_i → ask_size = 0

### 2.2 伪代码

```python
POS_LIMIT = 10

def pebbles_quote(positions, order_books, fair_prices):
    quotes = []
    for i in range(5):
        q = positions[i]
        bb = order_books[i].best_bid
        ba = order_books[i].best_ask
        fair = fair_prices[i]

        bid_price = bb + 1
        bid_size = POS_LIMIT - q
        if bid_price >= fair:
            bid_size = 0

        ask_price = ba - 1
        ask_size = POS_LIMIT + q
        if ask_price <= fair:
            ask_size = 0

        quotes.append({
            'bid_price': bid_price, 'bid_size': bid_size,
            'ask_price': ask_price, 'ask_size': ask_size,
        })
    return quotes
```

---

## 3. 对冲模块（take）

### 3.1 目标函数

$$\min \quad \|R'\|^2 + \beta \cdot |e'|$$

β = 0.1。

### 3.2 可行域

5 条腿内层方向一致，订单量 v：

- side = +1（内层卖单 → 我方买入）：i' ∈ [i, min(i + v, 10)]
- side = -1（内层买单 → 我方卖出）：i' ∈ [max(i - v, -10), i]

### 3.3 算法

固定 e' 后，4 条非 XL 腿的最优解为 clip(e', lo, hi)。外层枚举 e' 即可，复杂度 O(v)。

### 3.4 伪代码

```python
def pebbles_take(positions, v, side, beta=0.1):
    a, b, c, d, e = positions

    if side == +1:
        ranges = [(i, min(i + v, POS_LIMIT)) for i in (a, b, c, d, e)]
    else:
        ranges = [(max(i - v, -POS_LIMIT), i) for i in (a, b, c, d, e)]

    (a_lo, a_hi), (b_lo, b_hi), (c_lo, c_hi), (d_lo, d_hi), (e_lo, e_hi) = ranges

    best_cost, best = None, None
    for e_new in range(e_lo, e_hi + 1):
        a_new = clip(e_new, a_lo, a_hi)
        b_new = clip(e_new, b_lo, b_hi)
        c_new = clip(e_new, c_lo, c_hi)
        d_new = clip(e_new, d_lo, d_hi)

        risk_sq = (a_new - e_new)**2 + (b_new - e_new)**2 \
                + (c_new - e_new)**2 + (d_new - e_new)**2
        cost = risk_sq + beta * abs(e_new)

        if best_cost is None or cost < best_cost:
            best_cost, best = cost, (a_new, b_new, c_new, d_new, e_new)

    a_new, b_new, c_new, d_new, e_new = best
    return (a_new - a, b_new - b, c_new - c, d_new - d, e_new - e)


def clip(x, lo, hi):
    return max(lo, min(x, hi))
```

---

## 4. 调用顺序

每个 tick：

1. 若 5 条腿内层方向一致，调用 `pebbles_take` 并下发 deltas
2. 撤旧外层挂单
3. 调用 `pebbles_quote` 下发新挂单

**take 必须先于 quote**，因为 take 改变 positions，影响 quote 的 size。

---

## 5. 验证用例

### Take

| positions | v | side | 预期 deltas |
|---|---|---|---|
| (1,0,0,0,0) | 2 | -1 | (-1,0,0,0,0) |
| (0,0,0,0,1) | 2 | -1 | (0,0,0,0,-1) |
| (1,1,1,1,0) | 2 | +1 | (0,0,0,0,+1) |
| (3,-2,0,0,0) | 3 | -1 | (-3,0,-1,-1,-1) |
| (0,0,0,0,0) | 5 | +1 | (0,0,0,0,0) |

### Quote（fair = 10000）

| q_i | best_bid | best_ask | bid | ask |
|---|---|---|---|---|
| 0 | 9995 | 10005 | 9996×10 | 10004×10 |
| +10 | 9995 | 10005 | ×0 | 10004×20 |
| 0 | 9999 | 10005 | ×0 (10000≥fair) | 10004×10 |
| 0 | 9995 | 10001 | 9996×10 | ×0 (10000≤fair) |
