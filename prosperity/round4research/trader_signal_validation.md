# Trader 信号验证方法

## 目标

在每个 `(product, EMA 偏离 bucket)` 状态下,判断"引入 trader 成交信号"是否比"仅用均值回归"产生更高的前瞻收益。

---

## 1. 数据准备

### 1.1 每个 tick 记录

对每个 product、每个 timestamp,记录:

| 字段 | 计算方式 |
|---|---|
| `mid` | 沿用wall mid的计算方法 |
| `ema` | `mid` 的 EMA,周期可调节，暂定200 |
| `deviation` | `(mid - ema) / ema` |

**注意**:所有价格计算用 `mid`,不用成交价,以排除 spread 影响。

### 1.2 每笔 trader 成交记录

对 `state.market_trades` 中每笔成交,记录:

| 字段 | 说明 |
|---|---|
| `trader_id` | 交易者名字 |
| `product` | 产品 |
| `timestamp` | 成交 timestamp |
| `direction` | 买为 +1,卖为 -1(从 buyer/seller 字段判断) |
| `mid_at_trade` | 成交时该 product 的 mid |
| `ema_at_trade` | 成交时该 product 的 ema |
| `deviation_at_trade` | `(mid_at_trade - ema_at_trade) / ema_at_trade` |

---

## 2. 被解释变量:100-tick 前瞻收益

对每个事件(可以是 trader 成交,也可以是无条件的每个 tick):

```
forward_return_100 = (mid[t + 100] - mid[t]) / mid[t]
```

按 trader 方向 signed:

```
signed_return = direction × forward_return_100
```

无条件 benchmark 没有 direction,需要单独定义(见 §4)。

**等权,不按 size 加权。**

---

## 3. Bucket 切分

### 3.1 EMA 偏离 bucket

对每个 product 独立,用全样本 `deviation` 的分位数切 5 档:

| Bucket | 范围 | 含义 |
|---|---|---|
| B1 | [0%, 20%) | 偏离很负(均值回归该买) |
| B2 | [20%, 40%) | 偏离偏负 |
| B3 | [40%, 60%) | 中性 |
| B4 | [60%, 80%) | 偏离偏正 |
| B5 | [80%, 100%] | 偏离很正(均值回归该卖) |

每个 product 的分位数边界用全样本计算,**不要跨 product 共用边界**。

### 3.2 Trader 方向 vs bucket 方向

定义 bucket 隐含的均值回归方向:

| Bucket | mr_direction |
|---|---|
| B1, B2 | +1(买) |
| B3 | 0(无方向) |
| B4, B5 | -1(卖) |

对每笔 trader 成交,新增字段:

```
alignment = "aligned" if trader_direction == mr_direction
            else "opposed" if trader_direction == -mr_direction
            else "neutral"  # B3 时
```

---

## 4. 三组指标计算

对每个 `(trader_id, product, bucket)` 三元组,计算三个数:

### 4.1 Benchmark(无信号,均值回归 baseline)

在该 bucket 内,**所有 tick**(无论 trader 是否成交):

```
benchmark = mean over all ticks t in bucket of:
    mr_direction × forward_return_100(t)
```

B3 的 `mr_direction = 0`,benchmark 直接取 0 或不参与比较。

### 4.2 Conditional Aligned

该 bucket 内,trader 在此刻成交且方向与 bucket 一致的子集:

```
conditional_aligned = mean over (trader trades in bucket where alignment=="aligned") of:
    trader_direction × forward_return_100  # 等价于 mr_direction × forward_return_100
```

### 4.3 Conditional Opposed

该 bucket 内,trader 在此刻成交且方向与 bucket 相反的子集:

```
conditional_opposed = mean over (trader trades in bucket where alignment=="opposed") of:
    trader_direction × forward_return_100  # 注意:这里是 trader 方向,不是 mr 方向
```

---

## 5. 判定逻辑

对每个 `(trader_id, product, bucket)`:

| 比较 | 结论 |
|---|---|
| `conditional_aligned > benchmark` 显著 | trader 是"确认信号",可在均值回归基础上加仓 |
| `conditional_aligned ≈ benchmark` | trader 无增量价值,忽略 |
| `conditional_opposed > 0` 显著 | trader 是"否决信号",反向时应跟 trader 而非均值回归 |
| `conditional_opposed ≈ 0 或 < 0` | trader 反向时无信息,继续按均值回归走 |

显著性用 bootstrap 95% CI,或者 t 检验 p < 0.05。

**样本量门槛**:任一 conditional 子集少于 30 笔成交的 bucket 标记为 "insufficient",不参与决策。

---

## 6. 输出表结构

最终一张长表,字段:

| 列 | 说明 |
|---|---|
| `trader_id` | |
| `product` | |
| `bucket` | B1..B5 |
| `n_ticks_in_bucket` | benchmark 样本量 |
| `n_aligned` | aligned 子集样本量 |
| `n_opposed` | opposed 子集样本量 |
| `benchmark` | §4.1 |
| `conditional_aligned` | §4.2 |
| `conditional_opposed` | §4.3 |
| `aligned_minus_benchmark` | aligned 增量 |
| `aligned_ci_low`, `aligned_ci_high` | bootstrap 95% CI |
| `opposed_ci_low`, `opposed_ci_high` | bootstrap 95% CI |
| `decision` | confirm / ignore / fade_mr / insufficient |

---

## 7. 实施顺序

1. 跑出每个 product 每个 tick 的 `(mid, ema, deviation)` 时间序列
2. 计算每个 product 的 deviation 分位数边界
3. 对 `state.market_trades` 打上 bucket 和 alignment 标签
4. 计算 `forward_return_100`(注意 t+100 越界的 tick 丢弃)
5. 按 §4 聚合三组指标
6. Bootstrap CI
7. 输出 §6 长表

每一步独立验证后再进下一步,中间结果落盘。

---

## 8. 关键约束

- **价格用 mid,不用成交价**
- **等权,不按 size 加权**
- **EMA 周期与现有策略一致,不新引入**
- **每个 product 独立切分位数**
- **样本量 < 30 的 bucket 不参与决策**
