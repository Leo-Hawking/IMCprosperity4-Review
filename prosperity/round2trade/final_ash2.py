from __future__ import annotations

import json
import math

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


TARGET = "ASH_COATED_OSMIUM"

Q_MAX = 80
TICK = 1
MU = 10000.0
SIGMA_P = 4.8

# fair parameters (reused from prototype)
VOL_THRESHOLD = 20.0
MAX_STALE_MS = 3000
HALF_SPREAD_CONST = 10.0
INNER_PRIOR_OFFSET = -0.5
INNER_CONFLICT_TOL = 0.75
INNER_OFFSET_MIN = -2.0
INNER_OFFSET_MAX = 1.0

# v2-specific parameters
Z_SAT = 1.3
DELTA_Q_TARGET = 21        # |Δq| < 10 -> target tier
DELTA_Q_EXTREME = 34         # |Δq| >= 60 -> extreme tier
DELTA_Q_ULTRA = 79           # |Δq| >= 72 -> ultra-extreme tier
BASE_MM_SIZE = 12            # ε 10-14
INNER_ZONE = 5               # |px - inner_fair| < 5 is inner


# Tier behavior table.
# (outer_buy, outer_sell, inner_buy_limit, inner_sell_limit, buy_take, sell_take)
# 遵循 §6 硬性规则："外层永远双向挂限价单（跨全部五档）"。
TIER_BEHAVIOR = {
    "ultra_long":       (True, False, True,  False, True,  False),
    "extreme_long":     (True, True, True,  False, True,  False),
    "nonextreme_long":  (True, True, False, False, True,  False),
    "target":           (True, True, True,  True,  True,  True),
    "nonextreme_short": (True, True, False, False, False, True),
    "extreme_short":    (True, True, False, True,  False, True),
    "ultra_short":      (False, True, False, True,  False, True),
}


# --- Prototype-reused helpers -------------------------------------------------

def _load_memory(trader_data: str) -> dict:
    if not trader_data:
        return {}
    try:
        loaded = json.loads(trader_data)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _select_obs_price(side_orders: dict[int, int], is_bid: bool) -> tuple[int | None, int | None]:
    candidates = [(px, vol) for px, vol in side_orders.items() if abs(vol) > VOL_THRESHOLD]
    if not candidates:
        return None, None
    max_abs_vol = max(abs(vol) for _, vol in candidates)
    tied = [(px, vol) for px, vol in candidates if abs(vol) == max_abs_vol]
    if is_bid:
        px, vol = max(tied, key=lambda x: x[0])
    else:
        px, vol = min(tied, key=lambda x: x[0])
    return int(px), int(vol)


def _best_levels(order_depth):
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    bid1_vol = int(order_depth.buy_orders.get(best_bid, 0)) if best_bid is not None else None
    ask1_vol = int(order_depth.sell_orders.get(best_ask, 0)) if best_ask is not None else None
    return best_bid, bid1_vol, best_ask, ask1_vol


def _compute_outer_fair(ts: int, order_depth, memory: dict) -> float | None:
    bid_obs, _ = _select_obs_price(order_depth.buy_orders, is_bid=True)
    ask_obs, _ = _select_obs_price(order_depth.sell_orders, is_bid=False)

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
        if last_bid is not None and last_bid_ts is not None and ts - int(last_bid_ts) <= MAX_STALE_MS:
            use_bid = float(last_bid)

    if use_ask is None:
        last_ask = memory.get("last_ask_obs")
        last_ask_ts = memory.get("last_ask_obs_ts")
        if last_ask is not None and last_ask_ts is not None and ts - int(last_ask_ts) <= MAX_STALE_MS:
            use_ask = float(last_ask)

    outer = None
    if use_bid is not None and use_ask is not None:
        outer = (float(use_bid) + float(use_ask)) / 2.0
    elif use_ask is not None:
        inferred_bid = float(use_ask) - 2.0 * HALF_SPREAD_CONST
        outer = (inferred_bid + float(use_ask)) / 2.0
    elif use_bid is not None:
        inferred_ask = float(use_bid) + 2.0 * HALF_SPREAD_CONST
        outer = (float(use_bid) + inferred_ask) / 2.0
    else:
        best_bid, _, best_ask, _ = _best_levels(order_depth)
        if best_bid is not None and best_ask is not None:
            outer = (float(best_bid) + float(best_ask)) / 2.0
        elif memory.get("last_outer_fair") is not None:
            outer = float(memory["last_outer_fair"])

    if outer is not None:
        memory["last_outer_fair"] = outer
    return outer


def _offset_candidate(px: float, outer: float, baseline_inner: float) -> float | None:
    norm = px - baseline_inner
    if 0.5 <= norm <= 3.5:
        return px - 2.0 - outer
    if -3.5 <= norm <= -0.5:
        return px + 2.0 - outer
    return None


def _compute_inner_fair(outer_fair: float | None, order_depth) -> float | None:
    if outer_fair is None:
        return None

    baseline_offset = INNER_PRIOR_OFFSET
    baseline_inner = outer_fair + baseline_offset

    best_bid, bid1_vol, best_ask, ask1_vol = _best_levels(order_depth)
    candidates: list[tuple[float, float]] = []

    if best_bid is not None:
        off = _offset_candidate(float(best_bid), outer_fair, baseline_inner)
        if off is not None:
            candidates.append((off, abs(float(bid1_vol or 0))))

    if best_ask is not None:
        off = _offset_candidate(float(best_ask), outer_fair, baseline_inner)
        if off is not None:
            candidates.append((off, abs(float(ask1_vol or 0))))

    if not candidates:
        inner_offset = baseline_offset
    else:
        raw_offsets = [x[0] for x in candidates]
        if len(raw_offsets) >= 2 and (max(raw_offsets) - min(raw_offsets) > INNER_CONFLICT_TOL):
            inner_offset = baseline_offset
        else:
            total_weight = sum(w for _, w in candidates)
            if total_weight > 0:
                inner_offset = sum(off * w for off, w in candidates) / total_weight
            else:
                inner_offset = baseline_offset

    inner_offset = max(INNER_OFFSET_MIN, min(INNER_OFFSET_MAX, inner_offset))
    inner_offset = round(inner_offset * 2.0) / 2.0
    return outer_fair + inner_offset


# --- v2 position control ------------------------------------------------------

def _target_position(inner_fair: float) -> tuple[float, float]:
    z = (inner_fair - MU) / SIGMA_P
    clipped = max(-1.0, min(1.0, z / Z_SAT))
    return -Q_MAX * clipped, z


def _classify_tier(delta_q: float) -> str:
    if delta_q >= DELTA_Q_ULTRA:
        return "ultra_long"
    if delta_q >= DELTA_Q_EXTREME:
        return "extreme_long"
    if delta_q >= DELTA_Q_TARGET:
        return "nonextreme_long"
    if delta_q <= -DELTA_Q_ULTRA:
        return "ultra_short"
    if delta_q <= -DELTA_Q_EXTREME:
        return "extreme_short"
    if delta_q <= -DELTA_Q_TARGET:
        return "nonextreme_short"
    return "target"


# --- Zone / price helpers -----------------------------------------------------

def _zone_split(order_depth, inner_fair: float):
    inner_bid_lo = inner_fair - INNER_ZONE
    outer_ask_lo = inner_fair + INNER_ZONE
    inner_bids = [px for px in order_depth.buy_orders if inner_bid_lo < px < inner_fair]
    inner_asks = [px for px in order_depth.sell_orders if inner_fair < px < outer_ask_lo]
    outer_bids = [px for px in order_depth.buy_orders if px <= inner_bid_lo]
    outer_asks = [px for px in order_depth.sell_orders if px >= outer_ask_lo]
    return inner_bids, inner_asks, outer_bids, outer_asks


def _outer_bid_cap(inner_fair: float) -> int:
    return int(math.floor(inner_fair - INNER_ZONE))


def _outer_ask_floor(inner_fair: float) -> int:
    return int(math.ceil(inner_fair + INNER_ZONE))


# --- Take orders --------------------------------------------------------------

def _generate_take_orders(order_depth, q: int, inner_fair: float, delta_q: float,
                          tier: str) -> tuple[list[Order], int]:
    orders: list[Order] = []
    buy_take, sell_take = TIER_BEHAVIOR[tier][4], TIER_BEHAVIOR[tier][5]

    # Toward-direction budget per §9.1; target tier keeps prototype behavior.
    if tier == "target":
        buy_budget = max(Q_MAX - q, 0)
        sell_budget = max(Q_MAX + q, 0)
    elif delta_q > 0:
        buy_budget = min(max(Q_MAX - q, 0), int(delta_q))
        sell_budget = 0
    else:
        buy_budget = 0
        sell_budget = min(max(Q_MAX + q, 0), int(-delta_q))

    if buy_take and buy_budget > 0:
        for ask_px in sorted(order_depth.sell_orders.keys()):
            if ask_px >= inner_fair or buy_budget <= 0:
                break
            avail = max(-int(order_depth.sell_orders[ask_px]), 0)
            take_size = min(avail, buy_budget)
            if take_size > 0:
                orders.append(Order(TARGET, int(ask_px), int(take_size)))
                q += take_size
                buy_budget -= take_size

    if sell_take and sell_budget > 0:
        for bid_px in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_px <= inner_fair or sell_budget <= 0:
                break
            avail = max(int(order_depth.buy_orders[bid_px]), 0)
            take_size = min(avail, sell_budget)
            if take_size > 0:
                orders.append(Order(TARGET, int(bid_px), -int(take_size)))
                q -= take_size
                sell_budget -= take_size

    return orders, q


# --- Sizing -------------------------------------------------------------------

def _toward_budget_after_take(tier: str, delta_q: float, q_before: int,
                              q_after: int) -> int:
    """Remaining |Δq| budget after take (§9.1). Not meaningful for target tier."""
    if tier in ("ultra_long", "extreme_long", "nonextreme_long"):
        consumed = q_after - q_before
        return max(int(round(delta_q)) - consumed, 0)
    if tier in ("ultra_short", "extreme_short", "nonextreme_short"):
        consumed = q_before - q_after
        return max(int(round(-delta_q)) - consumed, 0)
    return 0


def _target_tier_side_sizes(delta_q: float) -> tuple[int, int]:
    """Per-layer (buy, sell) in target tier: toward=|Δq|, against=ε (§9.1/9.2)."""
    abs_dq = int(round(abs(delta_q)))
    if delta_q > 0:
        return abs_dq, BASE_MM_SIZE
    if delta_q < 0:
        return BASE_MM_SIZE, abs_dq
    return BASE_MM_SIZE, BASE_MM_SIZE


def _outer_target_sizes(tier: str, delta_q: float,
                        toward_remaining: int) -> tuple[int, int]:
    """Outer desired (buy_size, sell_size) before capacity clipping."""
    if tier == "target":
        return _target_tier_side_sizes(delta_q)
    if tier == "ultra_long":
        return toward_remaining, 0
    if tier == "ultra_short":
        return 0, toward_remaining
    if tier == "nonextreme_long":
        # inner does not carry; outer takes the whole remaining toward budget
        return toward_remaining, BASE_MM_SIZE
    if tier == "nonextreme_short":
        return BASE_MM_SIZE, toward_remaining
    # extreme tiers: reserve toward budget for inner (ε on outer toward side)
    if tier == "extreme_long":
        return min(BASE_MM_SIZE, toward_remaining), BASE_MM_SIZE
    if tier == "extreme_short":
        return BASE_MM_SIZE, min(BASE_MM_SIZE, toward_remaining)
    return 0, 0


def _inner_target_sizes(tier: str, delta_q: float, toward_remaining: int,
                        outer_buy_used: int, outer_sell_used: int) -> tuple[int, int]:
    """Inner desired (buy_size, sell_size) before capacity clipping."""
    if tier == "target":
        return _target_tier_side_sizes(delta_q)
    if tier == "ultra_long":
        return max(toward_remaining - outer_buy_used, 0), 0
    if tier == "ultra_short":
        return 0, max(toward_remaining - outer_sell_used, 0)
    if tier == "extreme_long":
        return max(toward_remaining - outer_buy_used, 0), 0
    if tier == "extreme_short":
        return 0, max(toward_remaining - outer_sell_used, 0)
    return 0, 0


# --- Outer limit orders (§7) --------------------------------------------------

def _outer_bid_price(inner_bids, outer_bids, inner_fair: float,
                     memory: dict) -> int | None:
    if inner_bids:
        return None
    cap = _outer_bid_cap(inner_fair)
    if outer_bids:
        candidate = max(outer_bids) + TICK
        if candidate > cap:
            candidate = cap
        return int(candidate) if candidate <= cap else None
    last = memory.get("last_outer_bid_px")
    if last is not None and int(last) <= cap:
        return int(last)
    return None


def _outer_ask_price(inner_asks, outer_asks, inner_fair: float,
                     memory: dict) -> int | None:
    if inner_asks:
        return None
    floor_px = _outer_ask_floor(inner_fair)
    if outer_asks:
        candidate = min(outer_asks) - TICK
        if candidate < floor_px:
            candidate = floor_px
        return int(candidate) if candidate >= floor_px else None
    last = memory.get("last_outer_ask_px")
    if last is not None and int(last) >= floor_px:
        return int(last)
    return None


def _generate_outer_orders(order_depth, q_after_take: int, inner_fair: float,
                           tier: str, delta_q: float, toward_remaining: int,
                           memory: dict,
                           inner_bids, inner_asks, outer_bids, outer_asks):
    orders: list[Order] = []
    outer_buy_allowed, outer_sell_allowed = TIER_BEHAVIOR[tier][0], TIER_BEHAVIOR[tier][1]

    buy_cap = max(Q_MAX - q_after_take, 0)
    sell_cap = max(Q_MAX + q_after_take, 0)

    buy_size_t, sell_size_t = _outer_target_sizes(tier, delta_q, toward_remaining)
    buy_used = 0
    sell_used = 0

    if outer_buy_allowed:
        px = _outer_bid_price(inner_bids, outer_bids, inner_fair, memory)
        if px is not None:
            size = min(buy_size_t, buy_cap)
            if size > 0:
                orders.append(Order(TARGET, px, int(size)))
                buy_used = int(size)
                memory["last_outer_bid_px"] = px

    if outer_sell_allowed:
        px = _outer_ask_price(inner_asks, outer_asks, inner_fair, memory)
        if px is not None:
            size = min(sell_size_t, sell_cap)
            if size > 0:
                orders.append(Order(TARGET, px, -int(size)))
                sell_used = int(size)
                memory["last_outer_ask_px"] = px

    return orders, buy_used, sell_used


# --- Inner limit orders (§8) --------------------------------------------------

def _generate_inner_orders(order_depth, q_after_take: int, inner_fair: float,
                           tier: str, delta_q: float, toward_remaining: int,
                           outer_buy_used: int, outer_sell_used: int,
                           inner_bids, inner_asks):
    orders: list[Order] = []
    inner_buy_allowed, inner_sell_allowed = TIER_BEHAVIOR[tier][2], TIER_BEHAVIOR[tier][3]

    if not (inner_buy_allowed or inner_sell_allowed):
        return orders

    buy_cap = max(Q_MAX - q_after_take - outer_buy_used, 0)
    sell_cap = max(Q_MAX + q_after_take - outer_sell_used, 0)

    buy_size_t, sell_size_t = _inner_target_sizes(tier, delta_q, toward_remaining,
                                                  outer_buy_used, outer_sell_used)

    if inner_buy_allowed and inner_bids:
        candidate = max(inner_bids) + TICK
        if candidate < inner_fair:
            size = min(buy_size_t, buy_cap)
            if size > 0:
                orders.append(Order(TARGET, int(candidate), int(size)))

    if inner_sell_allowed and inner_asks:
        candidate = min(inner_asks) - TICK
        if candidate > inner_fair:
            size = min(sell_size_t, sell_cap)
            if size > 0:
                orders.append(Order(TARGET, int(candidate), -int(size)))

    return orders


# --- Main trader --------------------------------------------------------------

class Trader:
    def run(self, state: TradingState):
        result = {symbol: [] for symbol in state.order_depths.keys()}
        memory = _load_memory(state.traderData)

        if TARGET not in state.order_depths:
            return result, 0, json.dumps(memory)

        order_depth = state.order_depths[TARGET]
        q = int(state.position.get(TARGET, 0))
        ts = int(state.timestamp)

        outer_fair = _compute_outer_fair(ts, order_depth, memory)
        inner_fair = _compute_inner_fair(outer_fair, order_depth)

        if inner_fair is None:
            memory["last_inner_fair"] = None
            return result, 0, json.dumps(memory)

        memory["last_inner_fair"] = inner_fair

        q_star, z = _target_position(inner_fair)
        delta_q = q_star - q
        tier = _classify_tier(delta_q)

        take_orders, q_after = _generate_take_orders(
            order_depth, q, inner_fair, delta_q, tier
        )

        toward_remaining = _toward_budget_after_take(tier, delta_q, q, q_after)

        inner_bids, inner_asks, outer_bids, outer_asks = _zone_split(order_depth, inner_fair)

        outer_orders, outer_buy_used, outer_sell_used = _generate_outer_orders(
            order_depth, q_after, inner_fair, tier, delta_q, toward_remaining, memory,
            inner_bids, inner_asks, outer_bids, outer_asks
        )

        inner_orders = _generate_inner_orders(
            order_depth, q_after, inner_fair, tier, delta_q, toward_remaining,
            outer_buy_used, outer_sell_used, inner_bids, inner_asks
        )

        result[TARGET] = take_orders + outer_orders + inner_orders
        return result, 0, json.dumps(memory)