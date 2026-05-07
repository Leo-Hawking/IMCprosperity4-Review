"""make_pebble.py — PEBBLES 做市 + 对冲

按照 PEBBLES_做市与对冲算法规范.md 实现：
  1) 每 tick 先调用 pebbles_take（5 腿内层方向一致时下达对冲单）
  2) 然后用更新后的 positions 调用 pebbles_quote 下达外层做市单

fair price 由 round5 的 wall-mid 规则给出（CLAUDE.md）。
"""

import json
from typing import Dict, List, Optional, Tuple

try:
    from datamodel import Order, OrderDepth, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, OrderDepth, TradingState


PRODUCTS = ["PEBBLES_XS", "PEBBLES_S", "PEBBLES_M", "PEBBLES_L", "PEBBLES_XL"]
POS_LIMIT = 10
BETA = 0.1


# --------------------------------------------------------------------------
# Fair price (round5 wall-mid rule)
# --------------------------------------------------------------------------

def _max_vol_side(levels: Dict[int, int]) -> Optional[int]:
    if not levels:
        return None
    best = None
    best_vol = -1
    for px, vol in levels.items():
        v = abs(vol)
        if v > best_vol:
            best_vol = v
            best = px
    return best


def _wall_mid(od: OrderDepth, prev: Optional[float]) -> Optional[float]:
    bid = _max_vol_side(od.buy_orders)
    ask = _max_vol_side(od.sell_orders)
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return prev


def _fair_price(od: OrderDepth, wall_mid: float) -> float:
    prices = list(od.buy_orders.keys()) + list(od.sell_orders.keys())
    near = [p for p in prices if abs(p - wall_mid) < 1]
    if len(near) == 1:
        return float(near[0])
    return wall_mid - 0.5


# --------------------------------------------------------------------------
# Quote module
# --------------------------------------------------------------------------

def pebbles_quote(positions, best_bids, best_asks, fairs):
    quotes = []
    for i in range(5):
        q = positions[i]
        bb = best_bids[i]
        ba = best_asks[i]
        fair = fairs[i]

        bid_price = bb + 1
        bid_size = POS_LIMIT - q
        if bid_price >= fair or bid_size <= 0:
            bid_size = 0

        ask_price = ba - 1
        ask_size = POS_LIMIT + q
        if ask_price <= fair or ask_size <= 0:
            ask_size = 0

        quotes.append((bid_price, bid_size, ask_price, ask_size))
    return quotes


# --------------------------------------------------------------------------
# Take module
# --------------------------------------------------------------------------

def _clip(x, lo, hi):
    return max(lo, min(x, hi))


def pebbles_take(positions, v, side, beta=BETA):
    a, b, c, d, e = positions
    if side == +1:
        ranges = [(i, min(i + v, POS_LIMIT)) for i in (a, b, c, d, e)]
    else:
        ranges = [(max(i - v, -POS_LIMIT), i) for i in (a, b, c, d, e)]

    (a_lo, a_hi), (b_lo, b_hi), (c_lo, c_hi), (d_lo, d_hi), (e_lo, e_hi) = ranges

    best_cost, best = None, (a, b, c, d, e)
    for e_new in range(e_lo, e_hi + 1):
        a_new = _clip(e_new, a_lo, a_hi)
        b_new = _clip(e_new, b_lo, b_hi)
        c_new = _clip(e_new, c_lo, c_hi)
        d_new = _clip(e_new, d_lo, d_hi)
        risk_sq = ((a_new - e_new) ** 2 + (b_new - e_new) ** 2
                   + (c_new - e_new) ** 2 + (d_new - e_new) ** 2)
        cost = risk_sq + beta * abs(e_new)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best = (a_new, b_new, c_new, d_new, e_new)

    a_new, b_new, c_new, d_new, e_new = best
    return (a_new - a, b_new - b, c_new - c, d_new - d, e_new - e)


# --------------------------------------------------------------------------
# Inner-direction classification
# --------------------------------------------------------------------------

def _inner_direction(od: OrderDepth, fair: float) -> Tuple[int, int, int, int]:
    """Return (side, take_volume_available, best_bid, best_ask).

    side: +1 if best_ask < fair (we'd buy), -1 if best_bid > fair (we'd sell), 0 otherwise.
    take_volume_available: shares offered/bid at that favorable inner price.
    """
    bb = max(od.buy_orders.keys()) if od.buy_orders else None
    ba = min(od.sell_orders.keys()) if od.sell_orders else None
    if bb is None or ba is None:
        return 0, 0, bb if bb is not None else 0, ba if ba is not None else 0

    if ba < fair:
        return +1, abs(od.sell_orders[ba]), bb, ba
    if bb > fair:
        return -1, abs(od.buy_orders[bb]), bb, ba
    return 0, 0, bb, ba


# --------------------------------------------------------------------------
# Trader
# --------------------------------------------------------------------------

class Trader:
    def run(self, state: TradingState):
        try:
            persistent = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            persistent = {}
        prev_walls: Dict[str, float] = persistent.get("walls", {})

        # Gather per-leg state.
        positions = []
        fairs = []
        best_bids = []
        best_asks = []
        sides = []
        avail_vols = []
        ods: List[Optional[OrderDepth]] = []

        for product in PRODUCTS:
            od = state.order_depths.get(product)
            ods.append(od)
            pos = state.position.get(product, 0)
            positions.append(pos)

            if od is None:
                fairs.append(None)
                best_bids.append(None)
                best_asks.append(None)
                sides.append(0)
                avail_vols.append(0)
                continue

            wm = _wall_mid(od, prev_walls.get(product))
            if wm is None:
                fairs.append(None)
                best_bids.append(None)
                best_asks.append(None)
                sides.append(0)
                avail_vols.append(0)
                continue

            prev_walls[product] = wm
            fair = _fair_price(od, wm)
            fairs.append(fair)

            side, vol, bb, ba = _inner_direction(od, fair)
            best_bids.append(bb)
            best_asks.append(ba)
            sides.append(side)
            avail_vols.append(vol)

        result: Dict[str, List[Order]] = {p: [] for p in PRODUCTS}

        # ---- 1) take (only if 5 legs share inner direction) -------------
        all_have_book = all(od is not None and f is not None
                            for od, f in zip(ods, fairs))
        if all_have_book and sides[0] != 0 and all(s == sides[0] for s in sides):
            side = sides[0]
            v = min(avail_vols)
            # cap v by remaining inventory headroom on every leg
            if side == +1:
                v = min(v, *(POS_LIMIT - p for p in positions))
            else:
                v = min(v, *(POS_LIMIT + p for p in positions))
            if v > 0:
                deltas = pebbles_take(tuple(positions), v, side)
                for product, delta, bb, ba in zip(PRODUCTS, deltas,
                                                  best_bids, best_asks):
                    if delta > 0:
                        result[product].append(Order(product, ba, delta))
                    elif delta < 0:
                        result[product].append(Order(product, bb, delta))
                # update positions to reflect projected fills before quoting
                positions = [p + d for p, d in zip(positions, deltas)]

        # ---- 2) quote ---------------------------------------------------
        quote_ok = all(bb is not None and ba is not None and f is not None
                       for bb, ba, f in zip(best_bids, best_asks, fairs))
        if quote_ok:
            quotes = pebbles_quote(positions, best_bids, best_asks, fairs)
            for product, (bid_px, bid_sz, ask_px, ask_sz) in zip(PRODUCTS, quotes):
                if bid_sz > 0:
                    result[product].append(Order(product, bid_px, bid_sz))
                if ask_sz > 0:
                    result[product].append(Order(product, ask_px, -ask_sz))

        result = {p: orders for p, orders in result.items() if orders}
        return result, 0, json.dumps({"walls": prev_walls})
