# VEV Voucher 均值回归策略实现文档

**比赛**：IMC Prosperity 4, Round 3 "Gloves Off"
**版本**：v1.0 (baseline)
**目标读者**：策略实现工程师

---

## 0. TL;DR

只交易 5 条 ATM 附近的 voucher，不交易 underlying，不做 delta hedge。
对每条 strike 独立运行：**BS 去结构 → EMA 去趋势 → 双阈值触发**。
全策略约 200 行 Python，无外部依赖（仅 numpy）。

---

## 1. 交易范围

### 1.1 交易（5 个产品）

| Symbol | Strike | 仓位上限 | 备注 |
|---|---|---|---|
| VEV_5000 | 5000 | 300 | 略 ITM，主力 |
| VEV_5100 | 5100 | 300 | ATM 附近，主力 |
| VEV_5200 | 5200 | 300 | ATM 附近，主力 |
| VEV_5300 | 5300 | 300 | ATM 附近，主力 |
| VEV_5500 | 5500 | 300 | OTM，平衡组合方向性 |

### 1.2 不交易（明确禁止）

| Symbol | 原因 |
|---|---|
| HYDROGEL_PACK | 不在本策略范围内 |
| VELVETFRUIT_EXTRACT (underlying) | 不主动开仓，**不 delta hedge** |
| VEV_4000 / VEV_4500 | Deep ITM，vega ≈ 0，IV 信号不干净 |
| VEV_5400 | 数据稀疏 / 流动性差 |
| VEV_6000 / VEV_6500 | Deep OTM，vega 太低，spread 吃光 alpha |

**实现要求**：在 strike 列表中只列出上述 5 个，其他产品根本不进入循环，不计算任何中间量。

---

## 2. 关键时间常量

### 2.1 TTE（Time To Expiry）

每个 round 对应 1 个日历日，凭证总寿命 7 天。Round 3 上线时的 TTE：

| 时点 | TTE |
|---|---|
| Round 1 起 | 7d |
| Round 2 起 | 6d |
| Round 3 起 | 5d |
| Round 3 终 | 4d |

历史回测数据涵盖：
- day 0（tutorial）：TTE 8d → 7d
- day 1（Round 1）：TTE 7d → 6d
- day 2（Round 2）：TTE 6d → 5d

### 2.2 TTE 实时计算

```python
TS_PER_DAY = 1_000_000   # 按平台实际值确认；图上 3 天 ≈ 2.8M ts，符合
DAYS_PER_YEAR = 365      # 与 BS 公式年化一致

def compute_TTE_days(global_ts: int, round_start_TTE: float) -> float:
    """live 时 round_start_TTE = 5.0；回测时根据所在 day 设定。"""
    progress_in_day = (global_ts % TS_PER_DAY) / TS_PER_DAY
    return round_start_TTE - progress_in_day

def compute_TTE_years(global_ts: int, round_start_TTE: float) -> float:
    return compute_TTE_days(global_ts, round_start_TTE) / DAYS_PER_YEAR
```

**TTE 单位约定**：所有 BS 内部计算用**年**为单位（`TTE_years`），其余地方（如 m_t）用**天**为单位（`TTE_days`）以保持公式与拟合时一致。**实现时务必区分**。

---

## 3. 数学组件

### 3.1 Black-Scholes Call（无风险利率 r = 0）

```python
from math import log, sqrt, exp, erf

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))

def bs_call_price(S: float, K: float, TTE_years: float, sigma: float) -> float:
    """欧式 call 价格，r = 0，q = 0。"""
    if TTE_years <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    sqrt_T = sqrt(TTE_years)
    d1 = (log(S / K) + 0.5 * sigma * sigma * TTE_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * norm_cdf(d1) - K * norm_cdf(d2)

def bs_call_delta(S: float, K: float, TTE_years: float, sigma: float) -> float:
    """仅在最后做风险监控时使用，不参与下单决策。"""
    if TTE_years <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    sqrt_T = sqrt(TTE_years)
    d1 = (log(S / K) + 0.5 * sigma * sigma * TTE_years) / (sigma * sqrt_T)
    return norm_cdf(d1)
```

### 3.2 iv_hat 全局拟合（已固化）

**已经离线拟合好的公式（不再每 tick 重拟合）**：

```
iv_hat(m_t) = 0.0119 * m_t + 0.2441

其中 m_t = ln(K / S_t) / sqrt(TTE_days)
```

注意：拟合时 TTE 以**天**为单位，调用 BS 时换成**年**。两个量在不同地方都要用，不要混。

```python
IV_HAT_SLOPE     = 0.0119
IV_HAT_INTERCEPT = 0.2441

def compute_m_t(S: float, K: float, TTE_days: float) -> float:
    return log(K / S) / sqrt(TTE_days)

def compute_iv_hat(S: float, K: float, TTE_days: float) -> float:
    m_t = compute_m_t(S, K, TTE_days)
    return IV_HAT_SLOPE * m_t + IV_HAT_INTERCEPT
```

---

## 4. 信号管线（每个 strike 独立运行）

### Step 1：BS 残差（去掉 S/T/moneyness 结构）

```python
iv_hat       = compute_iv_hat(S, K, TTE_days)
bs_theo      = bs_call_price(S, K, TTE_years, iv_hat)
mid_price    = (best_bid + best_ask) / 2.0
resid        = mid_price - bs_theo
```

`resid` 的含义：在"全局 smile + 当前 S, T"的预期价格之外，市场偏离了多少 SeaShells。

### Step 2：EMA 去趋势

维护 3 个 EMA：

```python
mu          = ema_update(prev_mu,         resid,                  span=SPAN_LONG)
abs_dev     = ema_update(prev_abs_dev,    abs(resid - mu),        span=SPAN_LONG)
recent_dev  = ema_update(prev_recent_dev, abs(resid - mu),        span=SPAN_SHORT)
```

EMA 更新公式：

```python
def ema_update(prev: float, new_val: float, span: int) -> float:
    alpha = 2.0 / (span + 1.0)
    return alpha * new_val + (1.0 - alpha) * prev
```

- `mu`：残差的慢漂移中心，**这是真正的"公允基线"**
- `abs_dev`：长期典型偏离尺度，用于 z-score 分母
- `recent_dev`：短期偏离尺度，用于 switch_mean 过滤

### Step 3：自适应 z-score

```python
EPS = 1e-6
score = (resid - mu) / max(abs_dev, EPS)
```

`score` 是无量纲量，跨 strike 跨日可比，所有 strike 共用同一套阈值。

---

## 5. 执行规则

### 5.1 主决策（双阈值 + switch_mean）

```python
THR_OPEN  = 1.5    # 开仓阈值（待回测调）
THR_CLOSE = 0.3    # 平仓阈值（必须 < THR_OPEN，差值不能太小）
MIN_DEV   = ?      # 用历史 recent_dev 的 30 分位定，回测前必须确定
LIMIT     = 300    # 每个 strike 仓位上限（= 比赛限额）

def decide_target(score, recent_dev, current_pos):
    tradeable = recent_dev > MIN_DEV   # 信号尺度不够时只平不开

    if tradeable and score >  THR_OPEN:    return -LIMIT
    if tradeable and score < -THR_OPEN:    return +LIMIT
    if abs(score) < THR_CLOSE:             return 0
    return current_pos                     # 中间区域不动
```

**逻辑说明**：

- `score > THR_OPEN`：mid 显著高于 BS 公允 → 期权偏贵 → **卖满**
- `score < -THR_OPEN`：mid 显著低于 BS 公允 → 期权偏便宜 → **买满**
- `|score| < THR_CLOSE`：回到中心 → **平仓出场**
- 中间区域：**保持现有仓位**，不要 churn
- `tradeable = False`：当近期偏离尺度太小（IV 异常稳定的时段），**只允许平仓不允许开新仓**，避免点差吃 alpha

### 5.2 下单实现（target → orders）

```python
def target_to_orders(target_pos, current_pos, best_bid, best_ask):
    delta = target_pos - current_pos
    if delta == 0:
        return []
    if delta > 0:
        # 需要买入：吃对手的 ask 或主动挂单
        return [Buy(price=best_ask, qty=delta)]
    else:
        return [Sell(price=best_bid, qty=-delta)]
```

**要点**：
- baseline 用 IOC 形式 cross spread，简单可靠
- 不要做"挂在 mid 价等成交"的逻辑——baseline 阶段必须执行确定，后期优化才考虑 passive maker
- 单次下单不超过仓位上限（剩余空间检查）

---

## 6. 参数表

| 参数 | 含义 | 默认值 | 调参方式 |
|---|---|---|---|
| `IV_HAT_SLOPE` | smile 拟合斜率 | 0.0119 | 已固化，不调 |
| `IV_HAT_INTERCEPT` | smile 拟合截距 | 0.2441 | 已固化，不调 |
| `SPAN_LONG` | 慢 EMA 窗口 | 50_000 ts | 取约 3-5× 残差半衰期，跑 ACF 确定 |
| `SPAN_SHORT` | 快 EMA 窗口 | 10_000 ts | 取 SPAN_LONG / 5 |
| `THR_OPEN` | 开仓 \|score\| 阈值 | 1.5 | grid search 1.0-2.5 |
| `THR_CLOSE` | 平仓 \|score\| 阈值 | 0.3 | grid search 0.0-0.6 |
| `MIN_DEV` | 触发 switch_mean 的 dev 下限 | (待定) | 历史 recent_dev 的 30 分位 |
| `LIMIT` | 每 strike 仓位上限 | 300 | 比赛硬限额，不调 |
| `TS_PER_DAY` | 每天 ts 数 | 1_000_000 | 按平台实际值确认 |
| `DAYS_PER_YEAR` | 年化基数 | 365 | 与拟合时一致 |

**调参流程**（必须按这个顺序）：
1. 先在历史数据上估每条 strike 的残差**半衰期** → 定 `SPAN_LONG`
2. 历史 recent_dev 分布 → 定 `MIN_DEV`
3. 固定上述两参数，grid search `(THR_OPEN, THR_CLOSE)`
4. 三天独立单日回测验证一致性

---

## 7. 状态管理

### 7.1 每 strike 状态字典

```python
@dataclass
class StrikeState:
    mu:              float = 0.0
    abs_dev:         float = 0.0
    recent_dev:      float = 0.0
    n_updates:       int   = 0      # 用于暖机判断
    last_resid:      float = 0.0    # 调试用
```

整体状态：`states: Dict[int, StrikeState] = {K: StrikeState() for K in [5000, 5100, 5200, 5300, 5500]}`

### 7.2 暖机（Warmup）

EMA 启动时偏差大，前期信号不可信。

```python
WARMUP_TICKS = 3 * SPAN_LONG       # 经验值

if state.n_updates < WARMUP_TICKS:
    # 只更新 EMA，不交易
    target = current_pos
else:
    target = decide_target(score, state.recent_dev, current_pos)
```

**首次调用 EMA**：用第一个 resid 直接初始化 mu，避免从 0 慢慢爬升：

```python
if state.n_updates == 0:
    state.mu = resid
    state.abs_dev = 0.0
    state.recent_dev = 0.0
else:
    state.mu          = ema_update(state.mu,         resid,                  SPAN_LONG)
    state.abs_dev     = ema_update(state.abs_dev,    abs(resid - state.mu),  SPAN_LONG)
    state.recent_dev  = ema_update(state.recent_dev, abs(resid - state.mu),  SPAN_SHORT)
state.n_updates += 1
```

### 7.3 跨 round 持久化

IMC 框架用 `traderData` 字符串在 tick 间序列化状态。每 tick：

```python
# 入口：反序列化
state_dict = json.loads(state.traderData) if state.traderData else {}
strike_states = {K: StrikeState(**state_dict.get(str(K), {})) for K in STRIKES}

# ... 主逻辑 ...

# 出口：序列化
traderData = json.dumps({str(K): asdict(s) for K, s in strike_states.items()})
return orders, conversions, traderData
```

---

## 8. Edge Cases

按优先级处理：

| 情况 | 处理 |
|---|---|
| `best_bid` 或 `best_ask` 缺失 | 跳过该 strike，**EMA 也不更新**（避免脏数据污染状态）|
| `mid` 与上 tick 跳变超过 5×abs_dev | 视为坏数据，跳过该 strike 这 tick |
| spread > 某阈值（如 5 SeaShells） | 跳过下单，**EMA 仍更新**（信号有效但 ROI 不够） |
| TTE_days <= 0.01 | 全部平仓，停止开新仓（临近到期 BS 数值不稳） |
| underlying S 缺失 | 跳过整个本 tick 的所有 voucher 决策 |
| 仓位限额触发拒单 | 框架会拒绝超额单；下单前自己检查 `target ∈ [-LIMIT, LIMIT]` |
| 一次 target 调整过大 | 单 tick 单边最大下单 = `LIMIT`，框架支持；若 partial fill，下 tick 自然续上 |

**关键防御**：所有 BS / log / sqrt 调用前检查输入合法性，任何异常都用 `try/except` 包住，**异常时返回当前仓位（不动）**而非 0（避免错误平仓）。

---

## 9. 主循环伪代码

```python
STRIKES = [5000, 5100, 5200, 5300, 5500]
ROUND_START_TTE = 5.0    # Round 3

def run(state):
    orders = {}
    
    # 1. 反序列化状态
    strike_states = load_states(state.traderData)
    
    # 2. 公共量
    S = get_underlying_mid(state, "VELVETFRUIT_EXTRACT")
    if S is None:
        return {}, 0, dump_states(strike_states)
    
    TTE_days  = compute_TTE_days(state.timestamp, ROUND_START_TTE)
    TTE_years = TTE_days / DAYS_PER_YEAR
    
    if TTE_days < 0.01:
        # 临近到期：全平
        for K in STRIKES:
            cur = state.position.get(f"VEV_{K}", 0)
            if cur != 0:
                orders[f"VEV_{K}"] = liquidate(cur, state.order_depths[f"VEV_{K}"])
        return orders, 0, dump_states(strike_states)
    
    # 3. 每 strike 处理
    for K in STRIKES:
        symbol = f"VEV_{K}"
        ss = strike_states[K]
        
        depth = state.order_depths.get(symbol)
        if depth is None or not depth.buy_orders or not depth.sell_orders:
            continue
        
        best_bid = max(depth.buy_orders.keys())
        best_ask = min(depth.sell_orders.keys())
        mid = (best_bid + best_ask) / 2
        
        # 信号
        iv_hat = compute_iv_hat(S, K, TTE_days)
        bs_theo = bs_call_price(S, K, TTE_years, iv_hat)
        resid = mid - bs_theo
        
        update_emas(ss, resid)
        
        # 暖机或下单
        cur = state.position.get(symbol, 0)
        if ss.n_updates < WARMUP_TICKS:
            target = cur
        else:
            score = (resid - ss.mu) / max(ss.abs_dev, EPS)
            target = decide_target(score, ss.recent_dev, cur)
        
        if target != cur:
            orders[symbol] = target_to_orders(target, cur, best_bid, best_ask)
    
    return orders, 0, dump_states(strike_states)
```

---

## 10. 回测验证协议

**baseline 通过的最低标准**：

1. **三天独立单日回测**：分别用 day 0、day 1、day 2 跑，**3 天 PnL 必须都 > 0**（量级允许差异）
2. **每条 strike 单独回测**：注释掉其他 strike，5 条**至少 4 条**单独 PnL > 0
3. **Sharpe 检查**：日内 PnL 时间序列的 Sharpe > 0.5（粗略门槛）
4. **最大回撤**：单日最大回撤 < 单日 PnL 均值的 50%
5. **仓位健康**：仓位时间序列不能长时间贴在 ±LIMIT（说明信号方向稳定单边，可能 mu 没追上漂移）

**任何一项不达标，先排查后再调参**。常见排查点：
- 第 1 项不达标 → SPAN_LONG 太短，mu 跟着信号跑了
- 第 2 项个别 strike 不行 → 该 strike 半衰期不同，需要单独 SPAN_LONG
- 第 5 项 → 或 SPAN_LONG 不够长，或 MIN_DEV 太低导致信号尺度小时还在开仓

---

## 11. 风险监控（运行时打印，不影响交易）

每 N tick 打印一次：

```python
total_delta = sum(
    state.position.get(f"VEV_{K}", 0) * 
    bs_call_delta(S, K, TTE_years, compute_iv_hat(S, K, TTE_days))
    for K in STRIKES
)
print(f"ts={state.timestamp} S={S:.2f} total_delta={total_delta:.1f} "
      f"positions={ {K: state.position.get(f'VEV_{K}', 0) for K in STRIKES} }")
```

**用途**：
- 看 `total_delta` 是否长时间偏离 0 太多 → 评估"不 hedge"决定的实际敞口
- 若发现 worst-case 单步 underlying 跳动 × |total_delta| 超过日均 PnL → **此时才考虑加 batched hedge**（plan B）

---

## 12. 明确 NOT TO DO 的事

按危险等级排序：

1. ❌ **每个 timestamp 重新拟合 smile**：会把可交易的时间相关信号自己吃掉
2. ❌ **delta hedge underlying**：未经成本核算的 hedge 在比赛中是负 EV
3. ❌ **THR_OPEN = THR_CLOSE**：会在阈值附近 churn 烧点差
4. ❌ **跨 strike 共用 mu / abs_dev**：每条 strike 的 IV 漂移独立，必须分开
5. ❌ **EMA 从 0 启动并立即开始交易**：暖机期信号完全不可信
6. ❌ **价格残差直接做 z-score（不减 BS_theo）**：S 一动就触发假信号
7. ❌ **遇到坏数据用 mid=0 或 mid=last 兜底**：直接 skip 该 tick 该 strike，让 EMA 也不更新
8. ❌ **在没拿到正 PnL baseline 之前调高级参数**：先跑通最简版，再增量

---

## 13. 上线 Checklist

- [ ] BS 公式单元测试（与已知数值表对照，如 S=K=100, σ=20%, T=1y → 价格 ≈ 7.97）
- [ ] iv_hat 公式在 m_t = 0 时返回 0.2441（自检）
- [ ] TTE 计算在 day 边界处连续（day 1 末 = 6.0d, day 2 初 = 6.0d，**不要重置成 7.0d**）
- [ ] 反/序列化 round-trip 测试（state → JSON → state，等价）
- [ ] 5 条 strike 全部跑通无异常
- [ ] 三天独立单日回测全部通过
- [ ] grid search 已完成 `(THR_OPEN, THR_CLOSE)` 调参
- [ ] 风险监控输出已打开

---

## 附录 A：参考公式速查

```
m_t           = ln(K / S_t) / sqrt(TTE_days)
iv_hat(m_t)   = 0.0119 * m_t + 0.2441
bs_theo       = BS_call(S_t, K, TTE_years, iv_hat)     # r = q = 0
resid         = mid - bs_theo
mu            = EMA(resid, SPAN_LONG)
abs_dev       = EMA(|resid - mu|, SPAN_LONG)
recent_dev    = EMA(|resid - mu|, SPAN_SHORT)
score         = (resid - mu) / abs_dev
tradeable     = recent_dev > MIN_DEV

target = -LIMIT  if tradeable and score >  THR_OPEN
target = +LIMIT  if tradeable and score < -THR_OPEN
target =  0      if |score| < THR_CLOSE
target =  cur    otherwise
```

## 附录 B：术语对照

| 术语 | 含义 |
|---|---|
| TTE | Time To Expiry，到期时间 |
| BS | Black-Scholes |
| IV | Implied Volatility，隐含波动率 |
| iv_hat | 全局 smile 拟合得到的 IV 估计 |
| residual / resid | mid - BS(iv_hat)，价格残差 |
| EMA | Exponential Moving Average |
| z-score | (x - mean) / std，标准化信号 |
| switch_mean | 信号尺度不够时停止开新仓的过滤机制 |
| churn | 在阈值线附近反复开平，被点差吃 PnL |
