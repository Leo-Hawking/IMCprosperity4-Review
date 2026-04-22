"""INTARIAN_PEPPER_ROOT 实战策略 v3.

实现 strategy_v3.md：
  Phase 1 建仓（q < MIN_POSITION）：
    吃 ask ≤ fair + BUILD_PREMIUM，bid 补仓，不挂 ask
  Phase 2 做市（q ≥ MIN_POSITION，一旦进入不回退）：
    - q > MIN_POSITION：挂 ask（量受 min(ASK_SIZE, q - MIN_POSITION) 封顶），
                       吃非理性 bid（bid > fair 且量 < IRR_TAKE_THRESHOLD）
    - q ≤ MIN_POSITION：挂 bid(N-q) 向 N 补仓；卖出行为全部禁用
    买入侧：任何 q < N 时都会吃 ask < fair，无额外限制

模型：
    p_fair(t) = int(MU · t + b_t)

    b_t 仅在当前 tick 双侧都有大单 (size ≥ MIN_VALID_SIZE) 时更新：
      - 冷启动：b_cand = (vb + va)/2 - MU*t
                若距最近 ANCHOR_STEP 倍数 ≤ ANCHOR_TOL → b0 = 该整数倍（锚定）
                否则                                    → b0 = b_cand
      - 稳态：  滚动 CMA 吸纳新的双侧 mid 观测（不再锚定）

    若当前 tick 非双侧大单：冷启动前 → 不交易，等下一 tick；
                           冷启动后 → 保持上一 b，不更新。
    不做硬编码兜底，不使用单侧 PAD 偏移（避免 CMA 永久保留结构性偏差）。

屏蔽订单簿 ⟹ 报价 = 对手最优价 ± ε

按 IMC Prosperity 提交格式：run(state) -> (orders_dict, conversions, trader_data)
"""

from __future__ import annotations

import json
import math

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


# ============================================================
# §0 参数
# ============================================================
PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]
TARGET = "INTARIAN_PEPPER_ROOT"
ASH_TARGET = "ASH_COATED_OSMIUM"

# ── 常量 ──
MU = 0.001
N = 80
TICK = 1
MIN_VALID_SIZE = 15   # size ≥ 此值算外层"大单"，仅这类 tick 参与 fair 估计
ANCHOR_STEP = 100     # 冷启动时 b 锚定到此步长的整数倍
ANCHOR_TOL = 2        # 冷启动 b 候选距最近 ANCHOR_STEP 倍数 ≤ 此值时才锚定

# ── 可调参数 ──
BUILD_PREMIUM = 7       # Phase 1 建仓溢价容忍 fixed不可调
MIN_POSITION = 75       # Phase 2 最低持仓底线；卖出行为不得使 q 低于此值
                        # 也是 Phase 1 → Phase 2 的切换阈值（q ≥ MIN_POSITION 进入 Phase 2）
ASK_SPREAD_MIN = 0      # Phase 2 ask 最低距 fair 的距离
ASK_SIZE = 5            # Phase 2 ask 挂单量（会被 q - MIN_POSITION 再次封顶）
IRR_TAKE_THRESHOLD = 15  # 非理性买单：对手量 < 此值才吃
BID_PREMIUM = 0         # Phase 2 bid ceiling = fair + BID_PREMIUM
PHASE2_SELL_MIN_DELTA = 2  # 仅 Phase 2：若卖价 < fair + delta，则撤掉卖单
PHASE2_BUY_MIN_DELTA = 0   # 仅 Phase 2：若买价 > fair - delta，则撤掉买单


# ============================================================
# ASH strategy params (merged from final_ash.py)
# ============================================================
ASH_Q_MAX = 80
ASH_TICK = 1
ASH_MU = 10000.0
ASH_SIGMA_P = 4.7227

# fair parameters (reused from prototype)
ASH_VOL_THRESHOLD = 20.0
ASH_MAX_STALE_MS = 3000
ASH_HALF_SPREAD_CONST = 10.0
ASH_INNER_PRIOR_OFFSET = -0.5
ASH_INNER_CONFLICT_TOL = 0.75
ASH_INNER_OFFSET_MIN = -2.0
ASH_INNER_OFFSET_MAX = 1.0

# v2-specific parameters
ASH_Z_SAT = 1.3
ASH_DELTA_Q_TARGET = 16
ASH_DELTA_Q_EXTREME = 60
ASH_BASE_MM_SIZE = 12
ASH_INNER_ZONE = 5


# Tier behavior table.
# (outer_buy, outer_sell, inner_buy_limit, inner_sell_limit, buy_take, sell_take)
ASH_TIER_BEHAVIOR = {
    "extreme_long": (True, True, True, False, True, False),
    "nonextreme_long": (True, True, False, False, True, False),
    "target": (True, True, True, True, True, True),
    "nonextreme_short": (True, True, False, False, False, True),
    "extreme_short": (True, True, False, True, False, True),
}


# ============================================================
# ASH strategy helpers
# ============================================================
def _ash_select_obs_price(side_orders: dict[int, int], is_bid: bool) -> tuple[int | None, int | None]:
    candidates = [(px, vol) for px, vol in side_orders.items() if abs(vol) > ASH_VOL_THRESHOLD]
    if not candidates:
        return None, None
    max_abs_vol = max(abs(vol) for _, vol in candidates)
    tied = [(px, vol) for px, vol in candidates if abs(vol) == max_abs_vol]
    if is_bid:
        px, vol = max(tied, key=lambda x: x[0])
    else:
        px, vol = min(tied, key=lambda x: x[0])
    return int(px), int(vol)


def _ash_best_levels(order_depth):
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    bid1_vol = int(order_depth.buy_orders.get(best_bid, 0)) if best_bid is not None else None
    ask1_vol = int(order_depth.sell_orders.get(best_ask, 0)) if best_ask is not None else None
    return best_bid, bid1_vol, best_ask, ask1_vol


def _ash_compute_outer_fair(ts: int, order_depth, memory: dict) -> float | None:
    bid_obs, _ = _ash_select_obs_price(order_depth.buy_orders, is_bid=True)
    ask_obs, _ = _ash_select_obs_price(order_depth.sell_orders, is_bid=False)

    if bid_obs is not None:
        memory["last_bid_obs"] = bid_obs
        memory["last_bid_obs_ts"] = ts
    if ask_obs is not None:
        memory["last_ask_obs"] = ask_obs
        memory["last_ask_obs_ts"] = ts

    use_bid = bid_obs
    use_ask = ask_obs

    if use_bid is None:
        last_bid = memory.get("last_bid_obs")
        last_bid_ts = memory.get("last_bid_obs_ts")
        if last_bid is not None and last_bid_ts is not None and ts - int(last_bid_ts) <= ASH_MAX_STALE_MS:
            use_bid = float(last_bid)

    if use_ask is None:
        last_ask = memory.get("last_ask_obs")
        last_ask_ts = memory.get("last_ask_obs_ts")
        if last_ask is not None and last_ask_ts is not None and ts - int(last_ask_ts) <= ASH_MAX_STALE_MS:
            use_ask = float(last_ask)

    outer = None
    if use_bid is not None and use_ask is not None:
        outer = (float(use_bid) + float(use_ask)) / 2.0
    elif use_ask is not None:
        inferred_bid = float(use_ask) - 2.0 * ASH_HALF_SPREAD_CONST
        outer = (inferred_bid + float(use_ask)) / 2.0
    elif use_bid is not None:
        inferred_ask = float(use_bid) + 2.0 * ASH_HALF_SPREAD_CONST
        outer = (float(use_bid) + inferred_ask) / 2.0
    else:
        best_bid, _, best_ask, _ = _ash_best_levels(order_depth)
        if best_bid is not None and best_ask is not None:
            outer = (float(best_bid) + float(best_ask)) / 2.0
        elif memory.get("last_outer_fair") is not None:
            outer = float(memory["last_outer_fair"])

    if outer is not None:
        memory["last_outer_fair"] = outer
    return outer


def _ash_offset_candidate(px: float, outer: float, baseline_inner: float) -> float | None:
    norm = px - baseline_inner
    if 0.5 <= norm <= 3.5:
        return px - 2.0 - outer
    if -3.5 <= norm <= -0.5:
        return px + 2.0 - outer
    return None


def _ash_compute_inner_fair(outer_fair: float | None, order_depth) -> float | None:
    if outer_fair is None:
        return None

    baseline_offset = ASH_INNER_PRIOR_OFFSET
    baseline_inner = outer_fair + baseline_offset

    best_bid, bid1_vol, best_ask, ask1_vol = _ash_best_levels(order_depth)
    candidates: list[tuple[float, float]] = []

    if best_bid is not None:
        off = _ash_offset_candidate(float(best_bid), outer_fair, baseline_inner)
        if off is not None:
            candidates.append((off, abs(float(bid1_vol or 0))))

    if best_ask is not None:
        off = _ash_offset_candidate(float(best_ask), outer_fair, baseline_inner)
        if off is not None:
            candidates.append((off, abs(float(ask1_vol or 0))))

    if not candidates:
        inner_offset = baseline_offset
    else:
        raw_offsets = [x[0] for x in candidates]
        if len(raw_offsets) >= 2 and (max(raw_offsets) - min(raw_offsets) > ASH_INNER_CONFLICT_TOL):
            inner_offset = baseline_offset
        else:
            total_weight = sum(w for _, w in candidates)
            if total_weight > 0:
                inner_offset = sum(off * w for off, w in candidates) / total_weight
            else:
                inner_offset = baseline_offset

    inner_offset = max(ASH_INNER_OFFSET_MIN, min(ASH_INNER_OFFSET_MAX, inner_offset))
    inner_offset = round(inner_offset * 2.0) / 2.0
    return outer_fair + inner_offset


def _ash_target_position(inner_fair: float) -> tuple[float, float]:
    z = (inner_fair - ASH_MU) / ASH_SIGMA_P
    clipped = max(-1.0, min(1.0, z / ASH_Z_SAT))
    return -ASH_Q_MAX * clipped, z


def _ash_classify_tier(delta_q: float) -> str:
    if delta_q >= ASH_DELTA_Q_EXTREME:
        return "extreme_long"
    if delta_q >= ASH_DELTA_Q_TARGET:
        return "nonextreme_long"
    if delta_q <= -ASH_DELTA_Q_EXTREME:
        return "extreme_short"
    if delta_q <= -ASH_DELTA_Q_TARGET:
        return "nonextreme_short"
    return "target"


def _ash_zone_split(order_depth, inner_fair: float):
    inner_bid_lo = inner_fair - ASH_INNER_ZONE
    outer_ask_lo = inner_fair + ASH_INNER_ZONE
    inner_bids = [px for px in order_depth.buy_orders if inner_bid_lo < px < inner_fair]
    inner_asks = [px for px in order_depth.sell_orders if inner_fair < px < outer_ask_lo]
    outer_bids = [px for px in order_depth.buy_orders if px <= inner_bid_lo]
    outer_asks = [px for px in order_depth.sell_orders if px >= outer_ask_lo]
    return inner_bids, inner_asks, outer_bids, outer_asks


def _ash_outer_bid_cap(inner_fair: float) -> int:
    return int(math.floor(inner_fair - ASH_INNER_ZONE))


def _ash_outer_ask_floor(inner_fair: float) -> int:
    return int(math.ceil(inner_fair + ASH_INNER_ZONE))


def _ash_generate_take_orders(order_depth, q: int, inner_fair: float, delta_q: float,
                              tier: str) -> tuple[list[Order], int]:
    orders: list[Order] = []
    buy_take, sell_take = ASH_TIER_BEHAVIOR[tier][4], ASH_TIER_BEHAVIOR[tier][5]

    if tier == "target":
        buy_budget = max(ASH_Q_MAX - q, 0)
        sell_budget = max(ASH_Q_MAX + q, 0)
    elif delta_q > 0:
        buy_budget = min(max(ASH_Q_MAX - q, 0), int(delta_q))
        sell_budget = 0
    else:
        buy_budget = 0
        sell_budget = min(max(ASH_Q_MAX + q, 0), int(-delta_q))

    if buy_take and buy_budget > 0:
        for ask_px in sorted(order_depth.sell_orders.keys()):
            if ask_px >= inner_fair or buy_budget <= 0:
                break
            avail = max(-int(order_depth.sell_orders[ask_px]), 0)
            take_size = min(avail, buy_budget)
            if take_size > 0:
                orders.append(Order(ASH_TARGET, int(ask_px), int(take_size)))
                q += take_size
                buy_budget -= take_size

    if sell_take and sell_budget > 0:
        for bid_px in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_px <= inner_fair or sell_budget <= 0:
                break
            avail = max(int(order_depth.buy_orders[bid_px]), 0)
            take_size = min(avail, sell_budget)
            if take_size > 0:
                orders.append(Order(ASH_TARGET, int(bid_px), -int(take_size)))
                q -= take_size
                sell_budget -= take_size

    return orders, q


def _ash_toward_budget_after_take(tier: str, delta_q: float, q_before: int,
                                  q_after: int) -> int:
    if tier in ("extreme_long", "nonextreme_long"):
        consumed = q_after - q_before
        return max(int(round(delta_q)) - consumed, 0)
    if tier in ("extreme_short", "nonextreme_short"):
        consumed = q_before - q_after
        return max(int(round(-delta_q)) - consumed, 0)
    return 0


def _ash_target_tier_side_sizes(delta_q: float) -> tuple[int, int]:
    abs_dq = int(round(abs(delta_q)))
    if delta_q > 0:
        return abs_dq, ASH_BASE_MM_SIZE
    if delta_q < 0:
        return ASH_BASE_MM_SIZE, abs_dq
    return ASH_BASE_MM_SIZE, ASH_BASE_MM_SIZE


def _ash_outer_target_sizes(tier: str, delta_q: float,
                            toward_remaining: int) -> tuple[int, int]:
    if tier == "target":
        return _ash_target_tier_side_sizes(delta_q)
    if tier == "nonextreme_long":
        return toward_remaining, ASH_BASE_MM_SIZE
    if tier == "nonextreme_short":
        return ASH_BASE_MM_SIZE, toward_remaining
    if tier == "extreme_long":
        return min(ASH_BASE_MM_SIZE, toward_remaining), ASH_BASE_MM_SIZE
    if tier == "extreme_short":
        return ASH_BASE_MM_SIZE, min(ASH_BASE_MM_SIZE, toward_remaining)
    return 0, 0


def _ash_inner_target_sizes(tier: str, delta_q: float, toward_remaining: int,
                            outer_buy_used: int, outer_sell_used: int) -> tuple[int, int]:
    if tier == "target":
        return _ash_target_tier_side_sizes(delta_q)
    if tier == "extreme_long":
        return max(toward_remaining - outer_buy_used, 0), 0
    if tier == "extreme_short":
        return 0, max(toward_remaining - outer_sell_used, 0)
    return 0, 0


def _ash_outer_bid_price(inner_bids, outer_bids, inner_fair: float,
                         memory: dict) -> int | None:
    if inner_bids:
        return None
    cap = _ash_outer_bid_cap(inner_fair)
    if outer_bids:
        candidate = max(outer_bids) + ASH_TICK
        if candidate > cap:
            candidate = cap
        return int(candidate) if candidate <= cap else None
    last = memory.get("last_outer_bid_px")
    if last is not None and int(last) <= cap:
        return int(last)
    return None


def _ash_outer_ask_price(inner_asks, outer_asks, inner_fair: float,
                         memory: dict) -> int | None:
    if inner_asks:
        return None
    floor_px = _ash_outer_ask_floor(inner_fair)
    if outer_asks:
        candidate = min(outer_asks) - ASH_TICK
        if candidate < floor_px:
            candidate = floor_px
        return int(candidate) if candidate >= floor_px else None
    last = memory.get("last_outer_ask_px")
    if last is not None and int(last) >= floor_px:
        return int(last)
    return None


def _ash_generate_outer_orders(order_depth, q_after_take: int, inner_fair: float,
                               tier: str, delta_q: float, toward_remaining: int,
                               memory: dict,
                               inner_bids, inner_asks, outer_bids, outer_asks):
    orders: list[Order] = []
    outer_buy_allowed, outer_sell_allowed = ASH_TIER_BEHAVIOR[tier][0], ASH_TIER_BEHAVIOR[tier][1]

    buy_cap = max(ASH_Q_MAX - q_after_take, 0)
    sell_cap = max(ASH_Q_MAX + q_after_take, 0)

    buy_size_t, sell_size_t = _ash_outer_target_sizes(tier, delta_q, toward_remaining)
    buy_used = 0
    sell_used = 0

    if outer_buy_allowed:
        px = _ash_outer_bid_price(inner_bids, outer_bids, inner_fair, memory)
        if px is not None:
            size = min(buy_size_t, buy_cap)
            if size > 0:
                orders.append(Order(ASH_TARGET, px, int(size)))
                buy_used = int(size)
                memory["last_outer_bid_px"] = px

    if outer_sell_allowed:
        px = _ash_outer_ask_price(inner_asks, outer_asks, inner_fair, memory)
        if px is not None:
            size = min(sell_size_t, sell_cap)
            if size > 0:
                orders.append(Order(ASH_TARGET, px, -int(size)))
                sell_used = int(size)
                memory["last_outer_ask_px"] = px

    return orders, buy_used, sell_used


def _ash_generate_inner_orders(order_depth, q_after_take: int, inner_fair: float,
                               tier: str, delta_q: float, toward_remaining: int,
                               outer_buy_used: int, outer_sell_used: int,
                               inner_bids, inner_asks):
    orders: list[Order] = []
    inner_buy_allowed, inner_sell_allowed = ASH_TIER_BEHAVIOR[tier][2], ASH_TIER_BEHAVIOR[tier][3]

    if not (inner_buy_allowed or inner_sell_allowed):
        return orders

    buy_cap = max(ASH_Q_MAX - q_after_take - outer_buy_used, 0)
    sell_cap = max(ASH_Q_MAX + q_after_take - outer_sell_used, 0)

    buy_size_t, sell_size_t = _ash_inner_target_sizes(tier, delta_q, toward_remaining,
                                                      outer_buy_used, outer_sell_used)

    if inner_buy_allowed and inner_bids:
        candidate = max(inner_bids) + ASH_TICK
        if candidate < inner_fair:
            size = min(buy_size_t, buy_cap)
            if size > 0:
                orders.append(Order(ASH_TARGET, int(candidate), int(size)))

    if inner_sell_allowed and inner_asks:
        candidate = min(inner_asks) - ASH_TICK
        if candidate > inner_fair:
            size = min(sell_size_t, sell_cap)
            if size > 0:
                orders.append(Order(ASH_TARGET, int(candidate), -int(size)))

    return orders


def _run_ash_strategy(state: TradingState, memory: dict) -> list[Order]:
    order_depth = state.order_depths.get(ASH_TARGET)
    if order_depth is None:
        return []

    q = int(state.position.get(ASH_TARGET, 0))
    ts = int(state.timestamp)

    outer_fair = _ash_compute_outer_fair(ts, order_depth, memory)
    inner_fair = _ash_compute_inner_fair(outer_fair, order_depth)

    if inner_fair is None:
        memory["last_inner_fair"] = None
        return []

    memory["last_inner_fair"] = inner_fair

    q_star, _ = _ash_target_position(inner_fair)
    delta_q = q_star - q
    tier = _ash_classify_tier(delta_q)

    take_orders, q_after = _ash_generate_take_orders(
        order_depth, q, inner_fair, delta_q, tier
    )

    toward_remaining = _ash_toward_budget_after_take(tier, delta_q, q, q_after)

    inner_bids, inner_asks, outer_bids, outer_asks = _ash_zone_split(order_depth, inner_fair)

    outer_orders, outer_buy_used, outer_sell_used = _ash_generate_outer_orders(
        order_depth, q_after, inner_fair, tier, delta_q, toward_remaining, memory,
        inner_bids, inner_asks, outer_bids, outer_asks
    )

    inner_orders = _ash_generate_inner_orders(
        order_depth, q_after, inner_fair, tier, delta_q, toward_remaining,
        outer_buy_used, outer_sell_used, inner_bids, inner_asks
    )

    return take_orders + outer_orders + inner_orders


# ============================================================
# §5 报价定位：屏蔽订单簿 ⟹ 最优对手价 ± ε，附真空回退
# ============================================================
def _my_bid_price(best_bid_opp, last_bid_px, best_ask_opp):
    if best_bid_opp is not None:
        px = best_bid_opp + TICK
    elif last_bid_px is not None:
        px = int(last_bid_px)
    else:
        return None
    if best_ask_opp is not None and px >= best_ask_opp:
        px = best_ask_opp - TICK
    return px if px > 0 else None


def _my_ask_price(best_ask_opp, last_ask_px, best_bid_opp):
    if best_ask_opp is not None:
        px = best_ask_opp - TICK
    elif last_ask_px is not None:
        px = int(last_ask_px)
    else:
        return None
    if best_bid_opp is not None and px <= best_bid_opp:
        px = best_bid_opp + TICK
    return px if px > 0 else None


def _best_valid_bid(od) -> int | None:
    valid = [p for p, sz in od.buy_orders.items() if int(sz) >= MIN_VALID_SIZE]
    return max(valid) if valid else None


def _best_valid_ask(od) -> int | None:
    valid = [p for p, sz in od.sell_orders.items() if -int(sz) >= MIN_VALID_SIZE]
    return min(valid) if valid else None


def _calc_fair_price(od, t: int, memory: dict) -> int | None:
    """计算 fair price = int(MU * t + b)，仅在当前 tick 双侧都有大单时更新 b。

    双侧大单 (size ≥ MIN_VALID_SIZE) 是无偏 mid 观测的唯一可靠来源。
    单侧观测需要 PAD 偏移校正，会系统性引入偏差；CMA 会把这类偏差永久保留，
    因此本实现完全避开单侧更新 —— 既消除了偏差源，也精简了代码。

    冷启动（仅一次，仅双侧大单 tick）：
      mid = (vb + va) / 2
      b_cand = mid - MU * t                                    # b 的原始候选
      b_anchor = round(b_cand / ANCHOR_STEP) * ANCHOR_STEP     # 最近的 100 倍数
      若 |b_cand - b_anchor| ≤ ANCHOR_TOL：
          b0 = b_anchor     # 贴紧整百，假设真实 b 就在整百上
      否则：
          b0 = b_cand       # 候选离整百太远，照原样用，交给 CMA 慢慢修

    稳态更新（CMA，仅双侧大单 tick，不再锚定）：
      b_t = b_{t-1} + ((mid_t - MU*t) - b_{t-1}) / n
    """
    fair_state = memory.setdefault("fair_state", {})

    vb = _best_valid_bid(od)
    va = _best_valid_ask(od)
    both_sides = vb is not None and va is not None

    # ── 冷启动 ──
    if not fair_state.get("initialized", False):
        if not both_sides:
            return None
        b_cand = 0.5 * (vb + va) - MU * t
        b_anchor = round(b_cand / ANCHOR_STEP) * ANCHOR_STEP
        if abs(b_cand - b_anchor) <= ANCHOR_TOL:
            b0 = float(b_anchor)
        else:
            b0 = float(b_cand)
        fair_state["initialized"] = True
        fair_state["b"] = b0
        fair_state["cma_n"] = 1
        return int(MU * t + b0)

    # ── 稳态：仅双侧大单 tick 才更新 CMA ──
    b = float(fair_state["b"])
    if both_sides:
        mid_t = 0.5 * (vb + va)
        n = int(fair_state["cma_n"]) + 1
        b = b + ((mid_t - MU * t) - b) / n
        fair_state["b"] = b
        fair_state["cma_n"] = n

    return int(MU * t + b)


# ============================================================
# §3 Phase 1：建仓
# ============================================================
def _phase1(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_asks):
    """吃 ask ≤ fair + BUILD_PREMIUM 直到满仓，bid 补仓，不挂 ask."""
    take_ceiling = p_fair + BUILD_PREMIUM
    buy_cap = N - q

    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price > take_ceiling or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    if buy_cap > 0:
        px = _my_bid_price(best_bid, memory.get("last_bid_px"), best_ask)
        if px is not None:
            orders.append(Order(TARGET, int(px), buy_cap))
            memory["last_bid_px"] = int(px)

    return q


# ============================================================
# §4 Phase 2：满仓做市
# ============================================================
def _phase2_take_irrational(od, q, p_fair, orders,
                            consumed_asks, consumed_bids):
    """4A 主动吃非理性单."""
    # ── 买入侧：吃所有 ask < fair ──
    buy_cap = N - q
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price >= p_fair or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    # ── 卖出侧：q > MIN_POSITION 时，吃 bid > fair 且对手量 < IRR_TAKE_THRESHOLD ──
    #          成交后 q 不得低于 MIN_POSITION
    sell_cap = q - MIN_POSITION
    if sell_cap > 0 and od.buy_orders:
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= p_fair or sell_cap <= 0:
                break
            avail = int(od.buy_orders[price]) - consumed_bids.get(price, 0)
            if avail <= 0 or avail >= IRR_TAKE_THRESHOLD:
                continue
            take = min(avail, sell_cap)
            orders.append(Order(TARGET, price, -take))
            consumed_bids[price] = consumed_bids.get(price, 0) + take
            q -= take
            sell_cap -= take

    return q


def _phase2_manage_ask(od, q, p_fair, best_bid, best_ask, orders, memory,
                      consumed_asks):
    """4B 挂 ask：不低于 fair + ASK_SPREAD_MIN；量受 q - MIN_POSITION 封顶。"""
    # 挂单量：不得使成交后 q 低于 MIN_POSITION（主动吃 bid 已消耗的额度也计入）
    ask_qty = min(ASK_SIZE, q - MIN_POSITION)
    if ask_qty <= 0:
        return

    # 重算 best_ask（扣除被消耗档位）
    if consumed_asks:
        rem = [p for p, v in od.sell_orders.items()
               if -int(v) - consumed_asks.get(p, 0) > 0]
        best_ask_eff = min(rem) if rem else None
    else:
        best_ask_eff = best_ask

    px = _my_ask_price(best_ask_eff, memory.get("last_ask_px"), best_bid)
    if px is None:
        return

    # 仅 Phase 2 生效：卖单价格太低（低于 fair + delta）则不挂单。
    if px < p_fair + PHASE2_SELL_MIN_DELTA:
        return

    ask_floor = p_fair + ASK_SPREAD_MIN
    if px < ask_floor:
        px = ask_floor
    orders.append(Order(TARGET, int(px), -ask_qty))
    memory["last_ask_px"] = int(px)


def _phase2_manage_bid(od, q, p_fair, best_bid, best_ask, orders, memory,
                      consumed_bids):
    """4C 非满仓挂 bid：不高于 fair + BID_PREMIUM，量 = N - q."""
    if consumed_bids:
        rem = [p for p, v in od.buy_orders.items()
               if int(v) - consumed_bids.get(p, 0) > 0]
        best_bid_eff = max(rem) if rem else None
    else:
        best_bid_eff = best_bid

    px = _my_bid_price(best_bid_eff, memory.get("last_bid_px"), best_ask)
    if px is None:
        return

    # 仅 Phase 2 生效：买单价格太高（高于 fair - delta）则不挂单。
    if px > p_fair - PHASE2_BUY_MIN_DELTA:
        return

    bid_ceiling = p_fair + BID_PREMIUM
    if px > bid_ceiling:
        px = bid_ceiling
    size = N - q
    if size > 0 and px > 0:
        orders.append(Order(TARGET, int(px), size))
        memory["last_bid_px"] = int(px)


# ============================================================
# Trader 主类（§1 主循环）
# ============================================================
class Trader:
    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {symbol: [] for symbol in state.order_depths.keys()}

        memory: dict = {}
        if state.traderData:
            try:
                loaded = json.loads(state.traderData)
                if isinstance(loaded, dict):
                    memory = loaded
            except Exception:
                memory = {}

        root_memory = memory.setdefault("root", {})
        if not isinstance(root_memory, dict):
            root_memory = {}
            memory["root"] = root_memory

        ash_memory = memory.setdefault("ash", {})
        if not isinstance(ash_memory, dict):
            ash_memory = {}
            memory["ash"] = ash_memory

        od = state.order_depths.get(TARGET)
        if od is not None:
            t = int(state.timestamp)
            q = max(int(state.position.get(TARGET, 0)), 0)
            p_fair = _calc_fair_price(od, t, root_memory)

            if p_fair is not None:
                best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
                best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

                root_orders: list[Order] = []
                consumed_asks: dict[int, int] = {}
                consumed_bids: dict[int, int] = {}

                phase = int(root_memory.get("phase", 1))
                if phase == 1 and q >= MIN_POSITION:
                    phase = 2
                root_memory["phase"] = phase

                if phase == 1:
                    q = _phase1(od, q, p_fair, best_bid, best_ask,
                                root_orders, root_memory, consumed_asks)
                else:
                    q = _phase2_take_irrational(od, q, p_fair, root_orders,
                                                consumed_asks, consumed_bids)
                    if q > MIN_POSITION:
                        _phase2_manage_ask(od, q, p_fair, best_bid, best_ask,
                                           root_orders, root_memory, consumed_asks)
                    else:
                        _phase2_manage_bid(od, q, p_fair, best_bid, best_ask,
                                           root_orders, root_memory, consumed_bids)

                result[TARGET] = root_orders

        result[ASH_TARGET] = _run_ash_strategy(state, ash_memory)
        return result, 0, json.dumps(memory)