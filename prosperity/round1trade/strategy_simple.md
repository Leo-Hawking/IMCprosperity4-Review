# ASH_COATED_OSMIUM 策略（Simple v1）

---

## 0. 策略定义

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
目标
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
围绕 inner_fair 做市：
1) 仅用主动吃单把仓位向 0 拉回（不靠主动吃单加仓）
2) 用双边最优价挂单获取价差

不使用 OU 回归信号，不区分内外层策略逻辑。
```

---

## 1. 参数与状态

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
常量
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Q_MAX = 80      // 单边仓位上限
TICK  = 1       // 最小价格单位

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
输入
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
inner_fair      // 当前时刻内层 fair（由 fair 模块给出）
orderbook       // 当前订单簿快照

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
状态
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
q ∈ [-Q_MAX, +Q_MAX]   // 当前净仓位
last_bid_price         // bid 真空锚点（对手 bid 侧无单时回退）
last_ask_price         // ask 真空锚点（对手 ask 侧无单时回退）
```

---

## 2. 主循环

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN_TICK():
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  inner_fair ← CALC_INNER_FAIR()
  if inner_fair is None:
    CANCEL(BID)
    CANCEL(ASK)
    return

  // A. 归零吃单（先执行，立即改变仓位）
  ZEROING_TAKE(inner_fair)

  // B. 双边挂单（基于 take 后的 q 与剩余订单簿）
  DUAL_PASSIVE_QUOTE(inner_fair)
```

执行顺序固定为 A -> B。原因是主动成交会先改变仓位与可见档位，挂单必须基于更新后的状态。

---

## 3. 策略一：归零吃单（Zeroing Take）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ZEROING_TAKE(inner_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  // q = 0: 不主动 take
  if q == 0:
    return

  // q < 0: 只买入低于 inner_fair 的 ask，目标回到 0
  if q < 0:
    need_to_zero ← -q
    buy_cap ← Q_MAX - q
    remaining ← min(need_to_zero, buy_cap)

    for ask in asks sorted by price ascending:
      if ask.price >= inner_fair or remaining <= 0:
        break
      take_size ← min(ABS_SIZE(ask), remaining)
      SEND_MARKET_ORDER(BUY, take_size, ask.price)
      q ← q + take_size
      remaining ← remaining - take_size

  // q > 0: 只卖出高于 inner_fair 的 bid，目标回到 0
  if q > 0:
    need_to_zero ← q
    sell_cap ← Q_MAX + q
    remaining ← min(need_to_zero, sell_cap)

    for bid in bids sorted by price descending:
      if bid.price <= inner_fair or remaining <= 0:
        break
      take_size ← min(ABS_SIZE(bid), remaining)
      SEND_MARKET_ORDER(SELL, take_size, bid.price)
      q ← q - take_size
      remaining ← remaining - take_size
```

### 3.1 规则解释

1. q < 0 时，仅允许买入（减空仓）。
2. q > 0 时，仅允许卖出（减多仓）。
3. 主动吃单不会扩大 $|q|$，只会把 $q$ 向 0 推进。
4. 扫描顺序使用最有利优先：ask 升序、bid 降序。

---

## 4. 策略二：双边挂单（Dual Passive Quote）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DUAL_PASSIVE_QUOTE(inner_fair):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  best_bid_opp ← BEST_BID_EXCLUDING_SELF()
  best_ask_opp ← BEST_ASK_EXCLUDING_SELF()

  // ── 1) Bid 挂单 ──
  my_bid ← None
  if best_bid_opp exists:
    my_bid ← best_bid_opp + TICK
    last_bid_price ← my_bid
  else if last_bid_price exists:
    my_bid ← last_bid_price

  if my_bid is not None:
    buy_size ← Q_MAX - q
    pass_inner_guard ← (my_bid < inner_fair)
    pass_cross_guard ← (best_ask_opp is None) or (my_bid < best_ask_opp)

    if buy_size > 0 and pass_inner_guard and pass_cross_guard:
      PLACE_OR_AMEND(BID, my_bid, buy_size)
    else:
      CANCEL(BID)
  else:
    CANCEL(BID)

  // ── 2) Ask 挂单 ──
  my_ask ← None
  if best_ask_opp exists:
    my_ask ← best_ask_opp - TICK
    last_ask_price ← my_ask
  else if last_ask_price exists:
    my_ask ← last_ask_price

  if my_ask is not None:
    sell_size ← Q_MAX + q
    pass_inner_guard ← (my_ask > inner_fair)
    pass_cross_guard ← (best_bid_opp is None) or (my_ask > best_bid_opp)

    if sell_size > 0 and pass_inner_guard and pass_cross_guard:
      PLACE_OR_AMEND(ASK, my_ask, sell_size)
    else:
      CANCEL(ASK)
  else:
    CANCEL(ASK)
```

### 4.1 保护条件

1. inner_fair 保护
   - bid: `my_bid < inner_fair`
   - ask: `my_ask > inner_fair`
2. 自交叉保护
   - `my_bid < best_ask_opp`
   - `my_ask > best_bid_opp`

---

## 5. 决策矩阵

| 当前仓位 | Ask 侧（买入方向） | Bid 侧（卖出方向） |
|---|---|---|
| q > 0 | 不主动买；挂 ask 可减仓 | 主动 take 高价 bid 减仓；同时可挂 bid 加仓 |
| q = 0 | 不主动 take；挂 ask 等成交 | 不主动 take；挂 bid 等成交 |
| q < 0 | 主动 take 低价 ask 减仓；同时可挂 ask 加仓 | 不主动卖；挂 bid 可减空仓 |

说明：take 与挂单可同 tick 并存，但归零吃单只在“有助于 $q \to 0$”方向触发。

---

## 6. 设计原则

1. 完全围绕 inner_fair：所有判断只依赖一个价格基准。
2. 主动吃单仅用于减仓：避免主动追价扩大风险敞口。
3. 被动双边打满容量：在屏蔽模型下，不占最优通常不成交，容量约束由仓位上限控制。
4. 严格不跨越 inner_fair：避免自己变成对手可直接套利的“非理性单”。
5. 先吃后挂：确保挂单基于最新仓位和剩余簿面。
