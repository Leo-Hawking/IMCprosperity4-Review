# SNACKPACK 做市与对冲策略规范

## 1. 适用范围

5 个资产：CHOCOLATE, VANILLA, PISTACHIO, STRAWBERRY, RASPBERRY。

## 2. 已定义变量

`fair_i`：每个资产的 fair price，由上游模块提供。

---

## 3. 做市模块（quote）

5 个资产逻辑一致：

- bid_price = best_bid + 1
- ask_price = best_ask - 1
- bid_size = 10 - q
- ask_size = 10 + q

**过滤**：

- bid_price ≥ fair → bid_size = 0
- ask_price ≤ fair → ask_size = 0

```python
POS_LIMIT = 10

def quote(q, best_bid, best_ask, fair):
    bid_price = best_bid + 1
    bid_size = POS_LIMIT - q
    if bid_price >= fair:
        bid_size = 0

    ask_price = best_ask - 1
    ask_size = POS_LIMIT + q
    if ask_price <= fair:
        ask_size = 0

    return {
        'bid_price': bid_price, 'bid_size': bid_size,
        'ask_price': ask_price, 'ask_size': ask_size,
    }
```

---

## 4. 对冲模块（take）

**通用约束**：所有 take 仅针对价格 == fair 的内层订单，绝不跨越 fair。

### 4.1 CHOCOLATE & VANILLA：配对收敛

目标：使两者持仓尽量相等且绝对值尽量小，等价于：

$$\min \quad (q_C' - q_V')^2 + \beta \cdot (q_C'^2 + q_V'^2)$$

β = 0.1。

可行域同 PEBBLES：

- side = +1（内层卖单 → 我方买入）：q' ∈ [q, min(q + v, 10)]
- side = -1（内层买单 → 我方卖出）：q' ∈ [max(q - v, -10), q]

```python
def take_choco_vanilla(qC, qV, vC, vV, sideC, sideV, beta=0.1):
    """
    sideC, sideV: 各自内层方向 (+1 卖单 / -1 买单 / 0 无内层)
    vC, vV: 各自内层订单量
    返回: (delta_C, delta_V)
    """
    def feasible_range(q, v, side):
        if side == +1:
            return range(q, min(q + v, POS_LIMIT) + 1)
        elif side == -1:
            return range(max(q - v, -POS_LIMIT), q + 1)
        else:
            return [q]

    rC = list(feasible_range(qC, vC, sideC))
    rV = list(feasible_range(qV, vV, sideV))

    best_cost, best = None, (qC, qV)
    for c_new in rC:
        for v_new in rV:
            cost = (c_new - v_new) ** 2 + beta * (c_new ** 2 + v_new ** 2)
            if best_cost is None or cost < best_cost:
                best_cost, best = cost, (c_new, v_new)

    return best[0] - qC, best[1] - qV
```

### 4.2 STRAWBERRY：能买就买

内层卖单出现时 take 至 limit。

```python
def take_strawberry(q, v, side):
    if side == +1:
        return min(v, POS_LIMIT - q)
    return 0
```

### 4.3 PISTACHIO：能卖就卖

内层买单出现时 take 至 limit。

```python
def take_pistachio(q, v, side):
    if side == -1:
        return -min(v, POS_LIMIT + q)
    return 0
```

### 4.4 RASPBERRY：基于 EMA 的方向性 take

维护超长周期 EMA：

- `mu` 初始值 10000
- 每 tick 更新：`mu = mu * (1 - α) + fair_R * α`，其中 α = 2 / (span + 1)，span = 4000
- threshold = 200

规则：

- fair_R > mu + threshold 且内层为卖单 → 买入
- fair_R < mu - threshold 且内层为买单 → 卖出

```python
SPAN = 4000
ALPHA = 2 / (SPAN + 1)
THRESHOLD = 200

def update_mu(mu, fair_R):
    return mu * (1 - ALPHA) + fair_R * ALPHA

def take_raspberry(q, v, side, fair_R, mu):
    # 树莓高于均值：预期回落，因此应该卖
    if fair_R > mu + THRESHOLD and side == -1:
        return -min(v, POS_LIMIT + q)

    # 树莓低于均值：预期反弹，因此应该买
    if fair_R < mu - THRESHOLD and side == +1:
        return min(v, POS_LIMIT - q)

    return 0
```

---

## 5. 调用顺序

每个 tick：

1. 更新 RASPBERRY 的 `mu`
2. 调用各资产的 take 模块（CHOCOLATE/VANILLA 联合调用）
3. 撤旧外层挂单
4. 调用 quote 下发新挂单

**take 必须先于 quote**。
