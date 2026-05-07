# Round 3 Voucher 策略验证 — Notebook 规格

## 目标

在动手写交易策略之前，用一个 notebook 把以下三件事验证清楚：

1. **VEV_4000 / 4500 能不能当作"扩展 delta 工具 + 被动 spread 收益来源"使用**
2. **中段 voucher（5000–5500）的 IV 是否具备可交易的 mean-reversion 性质**
3. **每个 voucher 的实测 delta 是否与 BS delta 一致，hedge ratio 用哪个**

输出是一份 verdict：哪些 voucher 进策略、用什么角色、用什么 hedge ratio。

---

## 输入数据

历史数据（3 个 round 拼接，按 `global_ts` 排序），每个 tick 至少包含：

- `global_ts`
- `VELVETFRUIT_EXTRACT` 的 `mid`、`best_bid`、`best_ask`、`bid_size`、`ask_size`
- 每个 `VEV_K`（K ∈ {4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500}）的 `mid`、`best_bid`、`best_ask`、`bid_size`、`ask_size`、（如有）`trade_price`、`trade_size`、`trade_side`
- 每个 voucher 的 mid-IV（已用 BS 反推好），列名 `iv_K`
- TTE（剩余天数，按 round 推算）

> 6000 / 6500 不进分析，bid/ask 卡在 0/1，IV 无意义。

---

## Section 1 — 基础设施

实现一个 BS 工具函数模块（不要重新造轮子，能 import 就 import）：

- `bs_price(S, K, T, sigma, r=0)` → call price
- `bs_delta(S, K, T, sigma, r=0)`
- `bs_vega(S, K, T, sigma, r=0)`
- `bs_iv_from_price(price, S, K, T, r=0)` → 反推 IV，对极端 ITM/OTM 做数值保护（找不到根时返回 NaN，不要抛异常）

所有时间单位统一用"年"，TTE 转换 `T = days / 365`。

加载数据后，先做一次 sanity check：画出 8 个 voucher 的 mid-IV 时序图叠在一起，确认数据没断、没有大段 NaN。

---

## Section 2 — VEV_4000 / 4500 的 delta 工具属性验证

**核心问题：4000 / 4500 是不是好用的 delta-1 工具？**

### 2.1 实测 delta 回归

对 K ∈ {4000, 4500} 各做一次：

```
ΔVEV_K = α + β · ΔVELVETFRUIT_EXTRACT + ε
```

- 用一阶差分（`mid` 的 tick-to-tick 变化）
- 报告 `β`（实测 delta）、`R²`、残差 std
- 同时画 BS delta（用每 tick 的 IV 算）的时间序列，和实测 `β` 对比

**判定标准：**
- 若 R² ≥ 0.95 且 β ≈ BS delta（误差 < 5%）→ 可作为 delta-1 工具
- 若 R² 在 0.7–0.95 → 是"脏" delta 工具，hedge 时要用实测 β，并预留残差风险
- 若 R² < 0.7 → 不要用它做 hedge

### 2.2 残差结构检查

对上一步的残差 `ε`：

- 画时序图、ACF
- 计算自相关系数 lag=1
- 看是否有波动聚集

**判定标准：**
- 残差自相关高 → 4000 有自己的独立 dynamics，hedge 残差不能忽略
- 残差近似白噪声 → 干净的 delta proxy

### 2.3 被动成交可达性（**这一步最重要**）

用历史 trade 数据（如果有 trade tape；如果只有 orderbook，跳过这一步并标注）：

- **被动成交频率**：在 best bid 上挂买单时，平均多少 tick 能成交一次？best ask 同理
- **挂单深度的影响**：如果挂在 mid 附近（不是最优价），成交概率多少
- **双边性**：4000 的成交在 buy / sell 两边是否平衡？如果严重单边（比如只有人卖给你），那不能作为对称的 delta 工具

输出一张表：

| voucher | passive fill rate (bid) | passive fill rate (ask) | 平均等待 tick | spread (ticks) |
|---------|------------------------|------------------------|--------------|----------------|
| 4000    | ?                      | ?                      | ?            | ?              |
| 4500    | ?                      | ?                      | ?            | ?              |
| VFE     | ?                      | ?                      | ?            | ?              |

**判定标准：**
- 4000 双边 fill rate 都 OK → 它是"零成本/正收益 delta 工具"，相当于把对冲容量从 ±200 扩展到 ±497
- 只有单边能被动成交 → 只能用作单方向 hedge，价值打折
- 几乎不能被动成交 → 退化成"贵的标的"，不要用

---

## Section 3 — 中段 voucher 的实测 delta（5000–5500）

对 K ∈ {5000, 5100, 5200, 5300, 5400, 5500} 各做：

### 3.1 实测 delta vs BS delta

同 2.1 的回归：

```
ΔVEV_K = α + β · ΔVELVETFRUIT_EXTRACT + ε
```

- 报告 `β`、`R²`
- 用每 tick 的 mid-IV 反推 BS delta，时序均值作为对比基准
- 画 `β`（rolling window，例如 10000 ticks）vs BS delta 的时序图

**判定标准：**
- 若实测 β 稳定且与 BS delta 接近 → hedge 用 BS delta（每 tick 重算）
- 若实测 β 系统性偏离 BS delta → 说明 voucher 定价含非 BS 因素，hedge 用实测 β

### 3.2 输出一张 hedge ratio 表

| voucher | BS delta (avg) | 实测 β | R² | 推荐 hedge ratio |
|---------|---------------|--------|-----|------------------|
| 5000    | ...           | ...    | ... | ...              |
| ...     | ...           | ...    | ... | ...              |

---

## Section 4 — IV mean-reversion 验证（决定哪些 voucher 是 alpha source）

**核心问题：哪些 voucher 的 IV 时序是 mean-reverting 的，半衰期多长？**

对 K ∈ {5000, 5100, 5200, 5300, 5400, 5500} 各做：

### 4.1 ADF / OU 检验

- 对 `iv_K(t)` 跑 ADF 单位根检验，记录 p-value
- 拟合 AR(1)：`iv(t) = a + ρ · iv(t-1) + ε`，记录 `ρ`
- 计算半衰期 `half_life = -ln(2) / ln(ρ)`（单位 tick）

### 4.2 输出回归性表

| voucher | ADF p-value | AR(1) ρ | 半衰期 (ticks) | 半衰期 (分钟，按 tick 频率换算) | 是否 mean-reverting |
|---------|-------------|---------|---------------|-------------------------------|---------------------|
| 5000    | ...         | ...     | ...           | ...                           | ...                 |
| ...     | ...         | ...     | ...           | ...                           | ...                 |

**判定标准：**
- ADF p < 0.05 且半衰期 < 几千 ticks → 是好的 mean-reversion 标的，进策略
- ADF p > 0.1 或半衰期 > 几万 ticks → 接近随机游走，**不要做 IV 均值回归**
- 半衰期太短（< 几十 ticks）→ 大概率是 microstructure noise，扣掉点差后没 edge

### 4.3 vega 与 IV 日波动

对每个 voucher 同时报告：

- 平均 vega（用平均 IV 和 平均 TTE 算）
- IV 的 daily std（或者每 round std）
- **预期 vega PnL std per day** = vega × IV daily std

这个数告诉我们持仓 1 单位 voucher 一天的 vol 风险有多大，用来做仓位预算。

---

## Section 5 — 最终 verdict 表

把前面所有结论汇总成一张总表，作为下一步写策略的输入：

| voucher | 角色 | hedge ratio | mean-reversion 半衰期 | vega per unit | 备注 |
|---------|------|-------------|----------------------|---------------|------|
| 4000    | delta 工具（被动 hedge） | ~1.0 | N/A | ~0 | 双边被动成交可达 |
| 4500    | ?    | ?           | ?                    | ?             | ?    |
| 5000    | vega alpha | ?     | ?                    | ?             | ?    |
| 5100    | ?    | ?           | ?                    | ?             | ?    |
| 5200    | ?    | ?           | ?                    | ?             | ?    |
| 5300    | ?    | ?           | ?                    | ?             | ?    |
| 5400    | ?    | ?           | ?                    | ?             | ?    |
| 5500    | ?    | ?           | ?                    | ?             | ?    |

**角色枚举：**
- `delta 工具` — 用来扩展 / 替代标的做对冲
- `vega alpha` — 主交易标的，赚 IV mean-reversion
- `不碰` — 不进策略

---

## Section 6 — Portfolio vega 预算（最后一步，决定仓位规模）

假设 verdict 表里有 N 个 vega alpha voucher。计算：

- 单 voucher 满仓（300 lot）的 vega PnL std per day
- 所有 alpha voucher 同方向满仓时的总 vega PnL std per day（保守起见，按相关系数 ~0.5 估，或者直接用 ΔIV 协方差矩阵算精确值）
- 对比 round 总 PnL 的目标量级

输出：**单 voucher 建议最大仓位**，使 portfolio 日 vega 风险不超过某个阈值（例如总账户的 X%，X 由团队决定）。

---

## 工程注意事项

- **频率**：所有回归、ADF、相关性都用同一频率的数据，建议 tick-level 直接做，不要重采样（重采样会引入伪相关）
- **拼接 round**：跨 round 的边界处差分要丢弃，避免引入跳跃
- **NaN 处理**：BS IV 反推失败的 tick 直接 drop，不要 fillna
- **不要画曲面**：上一轮已经验证全局曲面拟合在这个数据上无效，不要再做这件事
- **每个 section 输出一段中文 markdown 总结**，写明数字、结论、对策略的 implication

---

## 不在本 notebook 范围内

- 写交易策略代码
- 回测
- 提交到 Prosperity 平台

这个 notebook 只回答"该不该做、怎么做"的问题，策略实现是下一步。
