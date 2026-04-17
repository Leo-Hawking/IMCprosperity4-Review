# Fair 计算说明（当前版本）

本文档记录当前在 ash_micro 中使用的两套 fair：
- 外层 fair（outer fair）：用于挂单对齐、库存估值。
- 内层 fair（inner fair）：用于吃单判断，目标让内层更贴近 +2 / -2。

## 1. 关键参数

- REG_MIN_VOLUME = 15（仅非 ASH 回归路径使用）
- VOL_THRESHOLD = 20.0（外层选价时的最小量阈值）
- MAX_STALE_MS = 3000（外层同侧缓存有效期）
- HALF_SPREAD_CONST = 10.0（外层单边推断的固定 half spread）
- INNER_PRIOR_OFFSET = -0.5（内层初始基准偏移）
- INNER_CONFLICT_TOL = 0.75（内层双侧候选冲突阈值）
- INNER_OFFSET_MIN = -2.0（内层 offset 下界）
- INNER_OFFSET_MAX = 1.0（内层 offset 上界）

## 2. 外层 fair（outer fair）计算

### 2.1 单时刻侧价提取
对每个 timestamp，从 1~3 档中选择该侧价格：
1. 只保留 abs(volume) > VOL_THRESHOLD 的档位。
2. 找到该侧最大成交量 abs(volume)。
3. 若有并列：
   - bid 侧取更高价；
   - ask 侧取更低价。

得到：
- bid_obs
- ask_obs

### 2.2 初始化与缓存（开头逻辑）
状态变量初始为 None：
- last_bid, last_ask
- last_bid_ts, last_ask_ts
- last_outer_fair

缓存规则：
- 仅当当前时刻真实观测到 bid_obs / ask_obs 时，才更新对应缓存。
- 缓存有效条件：当前 ts 与该侧缓存 ts 之差 <= MAX_STALE_MS。

### 2.3 每个时刻 outer fair 决策
先得到：
- use_bid = bid_obs 或（缓存有效时 last_bid）
- use_ask = ask_obs 或（缓存有效时 last_ask）

然后按优先级：
1. 双侧可用：outer = (use_bid + use_ask) / 2
2. 仅 ask 可用：先推断 use_bid = use_ask - 2 * HALF_SPREAD_CONST，再取中点
3. 仅 bid 可用：先推断 use_ask = use_bid + 2 * HALF_SPREAD_CONST，再取中点
4. 双侧都不可用：
   - 有 mid_price 时，用 mid_price
   - 否则若 last_outer_fair 存在，沿用 last_outer_fair
   - 再否则为 None

这一步保证了起始阶段即便只有单侧，也能给出 outer fair（使用固定 half spread）。

## 3. 内层 fair（inner fair）计算

### 3.1 基线
当 outer fair 可用时：
- baseline_offset = -0.5
- baseline_inner_fair = outer + baseline_offset

### 3.2 吸附触发规则
对一档价进行标准化（相对 baseline inner）：
- bid1_norm = bid_price_1 - baseline_inner_fair
- ask1_norm = ask_price_1 - baseline_inner_fair

吸附区间：
- 若 norm 在 +0.5 到 +3.5，吸附到 +2
- 若 norm 在 -3.5 到 -0.5，吸附到 -2

对应候选 offset 反推：
- 吸附到 +2 时：offset = px - 2 - outer
- 吸附到 -2 时：offset = px + 2 - outer

其中 px 为触发吸附的一档价格（bid1 或 ask1）。

### 3.3 双侧融合与冲突回退
- 如果有多个候选 offset：按一档量的绝对值加权平均。
- 若双侧候选差异超过 INNER_CONFLICT_TOL：直接回退 baseline_offset（-0.5）。
- 若没有任何候选：也回退 baseline_offset（-0.5）。

### 3.4 约束与网格化
最终 inner_offset：
1. 先限制在 [INNER_OFFSET_MIN, INNER_OFFSET_MAX]
2. 再吸附到 0.5 网格（round(x * 2) / 2）

最后：
- inner fair = outer fair + inner_offset