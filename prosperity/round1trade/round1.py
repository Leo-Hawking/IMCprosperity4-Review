from __future__ import annotations

import json
import math

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


ASH_TARGET = "ASH_COATED_OSMIUM"
PEPPER_TARGET = "INTARIAN_PEPPER_ROOT"


# ============================================================
# ASH strategy (from 策略v2.py)
# ============================================================
ASH_Q_MAX = 80
ASH_TICK = 1
ASH_MU = 10000.0
ASH_SIGMA_P = 4.7227

ASH_VOL_THRESHOLD = 20.0
ASH_MAX_STALE_MS = 3000
ASH_HALF_SPREAD_CONST = 10.0
ASH_INNER_PRIOR_OFFSET = -0.5
ASH_INNER_CONFLICT_TOL = 0.75
ASH_INNER_OFFSET_MIN = -2.0
ASH_INNER_OFFSET_MAX = 1.0

ASH_Z_SAT = 1.5
ASH_DELTA_Q_TARGET = 10
ASH_DELTA_Q_EXTREME = 60
ASH_BASE_MM_SIZE = 10
ASH_INNER_ZONE = 5

# (outer_buy, outer_sell, inner_buy_limit, inner_sell_limit, buy_take, sell_take)
ASH_TIER_BEHAVIOR = {
    "extreme_long": (True, True, True, False, True, False),
    "nonextreme_long": (True, True, False, False, True, False),
    "target": (True, True, True, True, True, True),
    "nonextreme_short": (True, True, False, False, False, True),
    "extreme_short": (True, True, False, True, False, True),
}


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


def _run_ash(state: TradingState, memory: dict) -> list[Order]:
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

    take_orders, q_after = _ash_generate_take_orders(order_depth, q, inner_fair, delta_q, tier)
    toward_remaining = _ash_toward_budget_after_take(tier, delta_q, q, q_after)

    inner_bids, inner_asks, outer_bids, outer_asks = _ash_zone_split(order_depth, inner_fair)

    outer_orders, outer_buy_used, outer_sell_used = _ash_generate_outer_orders(
        order_depth,
        q_after,
        inner_fair,
        tier,
        delta_q,
        toward_remaining,
        memory,
        inner_bids,
        inner_asks,
        outer_bids,
        outer_asks,
    )

    inner_orders = _ash_generate_inner_orders(
        order_depth,
        q_after,
        inner_fair,
        tier,
        delta_q,
        toward_remaining,
        outer_buy_used,
        outer_sell_used,
        inner_bids,
        inner_asks,
    )

    return take_orders + outer_orders + inner_orders


# ============================================================
# PEPPER strategy (from best_pepper.py)
# ============================================================
PEP_MU = 0.001
PEP_N = 80
PEP_TICK = 1
PEP_ANCHOR_STEP = 100
PEP_ANCHOR_TOL = 5
PEP_MIN_VALID_SIZE = 15
PEP_ONE_SIDE_PAD = 8

PEP_BUILD_PREMIUM = 7
PEP_ASK_SPREAD_MIN = 0
PEP_ASK_SIZE = 8
PEP_IRR_TAKE_THRESHOLD = 8
PEP_BID_PREMIUM = 0


def _pep_my_bid_price(best_bid_opp, last_bid_px, best_ask_opp):
    if best_bid_opp is not None:
        px = best_bid_opp + PEP_TICK
    elif last_bid_px is not None:
        px = int(last_bid_px)
    else:
        return None
    if best_ask_opp is not None and px >= best_ask_opp:
        px = best_ask_opp - PEP_TICK
    return px if px > 0 else None


def _pep_my_ask_price(best_ask_opp, last_ask_px, best_bid_opp):
    if best_ask_opp is not None:
        px = best_ask_opp - PEP_TICK
    elif last_ask_px is not None:
        px = int(last_ask_px)
    else:
        return None
    if best_bid_opp is not None and px <= best_bid_opp:
        px = best_bid_opp + PEP_TICK
    return px if px > 0 else None


def _pep_round_to_step(x: float, step: int) -> int:
    return int(round(x / step) * step)


def _pep_first_multiple_in_open_interval(lo: int, hi: int, step: int) -> int | None:
    if lo >= hi:
        return None
    first = ((lo // step) + 1) * step
    return first if first < hi else None


def _pep_best_valid_bid(od) -> int | None:
    valid = [p for p, sz in od.buy_orders.items() if int(sz) >= PEP_MIN_VALID_SIZE]
    return max(valid) if valid else None


def _pep_best_valid_ask(od) -> int | None:
    valid = [p for p, sz in od.sell_orders.items() if -int(sz) >= PEP_MIN_VALID_SIZE]
    return min(valid) if valid else None


def _pep_calc_fair_price(od, t: int, memory: dict) -> int:
    fair_state = memory.setdefault("fair_state", {})
    initialized = bool(fair_state.get("initialized", False))

    if not initialized:
        fair_state["initialized"] = True
        fair_state["rolling"] = False
        fair_state["t0"] = t

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        b0: float | None = None
        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
            b_target = _pep_round_to_step(mid, PEP_ANCHOR_STEP)
            if abs(mid - b_target) <= PEP_ANCHOR_TOL:
                b0 = float(b_target)
            else:
                fair_state["rolling"] = True
        elif best_bid is not None:
            b_target = _pep_first_multiple_in_open_interval(
                int(best_bid), int(best_bid + 10), PEP_ANCHOR_STEP
            )
            if b_target is not None:
                b0 = float(b_target)
            else:
                b0 = float(best_bid + PEP_ONE_SIDE_PAD)
                fair_state["rolling"] = True
        elif best_ask is not None:
            b_target = _pep_first_multiple_in_open_interval(
                int(best_ask - 10), int(best_ask), PEP_ANCHOR_STEP
            )
            if b_target is not None:
                b0 = float(b_target)
            else:
                b0 = float(best_ask - PEP_ONE_SIDE_PAD)
                fair_state["rolling"] = True

        if b0 is None:
            b0 = 12000.0
            fair_state["rolling"] = True

        fair_state["b"] = b0
        fair_state["cma_n"] = 0

    b_prev = float(fair_state.get("b", 12000.0))

    valid_bid = _pep_best_valid_bid(od)
    valid_ask = _pep_best_valid_ask(od)

    if valid_bid is not None:
        fair_state["last_valid_bid"] = int(valid_bid)
    else:
        last_bid = fair_state.get("last_valid_bid")
        if last_bid is not None:
            valid_bid = int(last_bid)

    if valid_ask is not None:
        fair_state["last_valid_ask"] = int(valid_ask)
    else:
        last_ask = fair_state.get("last_valid_ask")
        if last_ask is not None:
            valid_ask = int(last_ask)

    mid_t: float | None = None
    if valid_bid is not None and valid_ask is not None:
        mid_t = 0.5 * (valid_bid + valid_ask)
    elif valid_bid is not None:
        mid_t = float(valid_bid + PEP_ONE_SIDE_PAD)
    elif valid_ask is not None:
        mid_t = float(valid_ask - PEP_ONE_SIDE_PAD)

    if fair_state.get("rolling", False) and mid_t is not None:
        n = int(fair_state.get("cma_n", 0)) + 1
        b_t = b_prev + ((mid_t - PEP_MU * t) - b_prev) / n
        fair_state["b"] = b_t
        fair_state["cma_n"] = n
        b_used = b_t
    else:
        b_used = b_prev

    memory["fair_state"] = fair_state
    return int(PEP_MU * t + b_used)


def _pep_phase1(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_asks):
    take_ceiling = p_fair + PEP_BUILD_PREMIUM
    buy_cap = PEP_N - q

    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price > take_ceiling or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(PEPPER_TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    if buy_cap > 0:
        px = _pep_my_bid_price(best_bid, memory.get("last_bid_px"), best_ask)
        if px is not None:
            orders.append(Order(PEPPER_TARGET, int(px), buy_cap))
            memory["last_bid_px"] = int(px)

    return q


def _pep_phase2_take_irrational(od, q, p_fair, orders,
                                consumed_asks, consumed_bids):
    buy_cap = PEP_N - q
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price >= p_fair or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(PEPPER_TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    if q >= PEP_N and od.buy_orders:
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= p_fair or q <= 0:
                break
            avail = int(od.buy_orders[price]) - consumed_bids.get(price, 0)
            if avail <= 0 or avail >= PEP_IRR_TAKE_THRESHOLD:
                continue
            take = min(avail, q)
            orders.append(Order(PEPPER_TARGET, price, -take))
            consumed_bids[price] = consumed_bids.get(price, 0) + take
            q -= take

    return q


def _pep_phase2_manage_ask(od, p_fair, best_bid, best_ask, orders, memory, consumed_asks):
    if consumed_asks:
        rem = [p for p, v in od.sell_orders.items() if -int(v) - consumed_asks.get(p, 0) > 0]
        best_ask_eff = min(rem) if rem else None
    else:
        best_ask_eff = best_ask

    px = _pep_my_ask_price(best_ask_eff, memory.get("last_ask_px"), best_bid)
    if px is None:
        return
    ask_floor = p_fair + PEP_ASK_SPREAD_MIN
    if px < ask_floor:
        px = ask_floor
    if PEP_ASK_SIZE > 0:
        orders.append(Order(PEPPER_TARGET, int(px), -PEP_ASK_SIZE))
        memory["last_ask_px"] = int(px)


def _pep_phase2_manage_bid(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_bids):
    if consumed_bids:
        rem = [p for p, v in od.buy_orders.items() if int(v) - consumed_bids.get(p, 0) > 0]
        best_bid_eff = max(rem) if rem else None
    else:
        best_bid_eff = best_bid

    px = _pep_my_bid_price(best_bid_eff, memory.get("last_bid_px"), best_ask)
    if px is None:
        return
    bid_ceiling = p_fair + PEP_BID_PREMIUM
    if px > bid_ceiling:
        px = bid_ceiling
    size = PEP_N - q
    if size > 0 and px > 0:
        orders.append(Order(PEPPER_TARGET, int(px), size))
        memory["last_bid_px"] = int(px)


def _run_pepper(state: TradingState, memory: dict) -> list[Order]:
    od = state.order_depths.get(PEPPER_TARGET)
    if od is None:
        return []

    t = int(state.timestamp)
    q = max(int(state.position.get(PEPPER_TARGET, 0)), 0)
    p_fair = _pep_calc_fair_price(od, t, memory)

    best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
    best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

    orders: list[Order] = []
    consumed_asks: dict[int, int] = {}
    consumed_bids: dict[int, int] = {}

    phase = int(memory.get("phase", 1))
    if phase == 1 and q >= PEP_N:
        phase = 2
    memory["phase"] = phase

    if phase == 1:
        q = _pep_phase1(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_asks)
    else:
        q = _pep_phase2_take_irrational(od, q, p_fair, orders, consumed_asks, consumed_bids)
        if q >= PEP_N:
            _pep_phase2_manage_ask(od, p_fair, best_bid, best_ask, orders, memory, consumed_asks)
        else:
            _pep_phase2_manage_bid(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_bids)

    return orders


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

        # Keep per-product memory independent to avoid key collisions.
        ash_memory = memory.setdefault("ash", {})
        pepper_memory = memory.setdefault("pepper", {})

        if ASH_TARGET in state.order_depths:
            result.setdefault(ASH_TARGET, [])
            result[ASH_TARGET] = _run_ash(state, ash_memory)

        if PEPPER_TARGET in state.order_depths:
            result.setdefault(PEPPER_TARGET, [])
            result[PEPPER_TARGET] = _run_pepper(state, pepper_memory)

        return result, 0, json.dumps(memory)