from __future__ import annotations

import json

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


TARGET = "ASH_COATED_OSMIUM"

Q_MAX = 80
TICK = 1
MU = 10000.0
K = 8
K_take = 1.5
K_quote = 8.5

VOL_THRESHOLD = 20.0
MAX_STALE_MS = 3000
HALF_SPREAD_CONST = 10.0
INNER_PRIOR_OFFSET = -0.5
INNER_CONFLICT_TOL = 0.75
INNER_OFFSET_MIN = -2.0
INNER_OFFSET_MAX = 1.0


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


def _q_star(price: float) -> float:
    z = (price - MU) / K
    z = max(-1.0, min(1.0, z))
    return -Q_MAX * z


def _generate_take_orders(order_depth, q: int, inner_fair: float) -> tuple[list[Order], int]:
    orders: list[Order] = []

    dev = inner_fair - MU
    if abs(dev) >= K_take:
        ask_threshold = MU
        bid_threshold = MU
    else:
        ask_threshold = min(MU, inner_fair)
        bid_threshold = max(MU, inner_fair)

    for ask_px in sorted(order_depth.sell_orders.keys()):
        if ask_px >= ask_threshold:
            break
        avail = max(-int(order_depth.sell_orders[ask_px]), 0)
        target = _q_star(float(ask_px))
        room_target = int(target - q)
        room_cap = Q_MAX - q
        take_size = min(avail, room_target, room_cap)
        if take_size > 0:
            orders.append(Order(TARGET, int(ask_px), int(take_size)))
            q += take_size

    for bid_px in sorted(order_depth.buy_orders.keys(), reverse=True):
        if bid_px <= bid_threshold:
            break
        avail = max(int(order_depth.buy_orders[bid_px]), 0)
        target = _q_star(float(bid_px))
        room_target = int(q - target)
        room_cap = Q_MAX + q
        sell_size = min(avail, room_target, room_cap)
        if sell_size > 0:
            orders.append(Order(TARGET, int(bid_px), -int(sell_size)))
            q -= sell_size

    return orders, q


def _generate_quote_orders(order_depth, q: int, inner_fair: float) -> list[Order]:
    orders: list[Order] = []
    best_bid, _, best_ask, _ = _best_levels(order_depth)

    deviation = inner_fair - MU
    extreme_long = deviation <= -K_quote
    extreme_short = deviation >= K_quote
    is_extreme = extreme_long or extreme_short

    place_buy = (not is_extreme) or extreme_long
    place_sell = (not is_extreme) or extreme_short

    if place_buy and best_bid is not None:
        px = int(best_bid) + TICK
        size = Q_MAX - q
        if size > 0 and px < MU:
            orders.append(Order(TARGET, px, int(size)))

    if place_sell and best_ask is not None:
        px = int(best_ask) - TICK
        size = Q_MAX + q
        if size > 0 and px > MU:
            orders.append(Order(TARGET, px, -int(size)))

    return orders


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

        take_orders, q_after = _generate_take_orders(order_depth, q, inner_fair)
        quote_orders = _generate_quote_orders(order_depth, q_after, inner_fair)

        result[TARGET] = take_orders + quote_orders
        return result, 0, json.dumps(memory)
