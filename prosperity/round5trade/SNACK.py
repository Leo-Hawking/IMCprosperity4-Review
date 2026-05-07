"""SNACK.py — 5 个 SNACKPACK 资产的做市与对冲

按 SNACKPACK_策略规范.md 实现：
  - CHOCOLATE & VANILLA：配对收敛 take
  - STRAWBERRY：能买就买
  - PISTACHIO：能卖就卖
  - RASPBERRY：长周期 EMA 方向性 take
  - 五者统一外层 quote

fair price 由 round5 wall-mid 规则给出（CLAUDE.md）。
"""

import json
from typing import Dict, List, Optional, Tuple

try:
    from datamodel import Order, OrderDepth, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, OrderDepth, TradingState


CHOCOLATE = "SNACKPACK_CHOCOLATE"
VANILLA = "SNACKPACK_VANILLA"
PISTACHIO = "SNACKPACK_PISTACHIO"
STRAWBERRY = "SNACKPACK_STRAWBERRY"
RASPBERRY = "SNACKPACK_RASPBERRY"
PRODUCTS = [CHOCOLATE, VANILLA, PISTACHIO, STRAWBERRY, RASPBERRY]

POS_LIMIT = 10
BETA = 0.1

SPAN = 4000
ALPHA = 2.0 / (SPAN + 1)
THRESHOLD = 200
RASPBERRY_INIT_MU = 10000.0


# --------------------------------------------------------------------------
# Fair price (round5 wall-mid rule)
# --------------------------------------------------------------------------

def _max_vol_price(levels: Dict[int, int]) -> Optional[int]:
    if not levels:
        return None
    best, best_vol = None, -1
    for px, vol in levels.items():
        v = abs(vol)
        if v > best_vol:
            best_vol, best = v, px
    return best


def _wall_mid(od: OrderDepth, prev: Optional[float]) -> Optional[float]:
    bid = _max_vol_price(od.buy_orders)
    ask = _max_vol_price(od.sell_orders)
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
# Inner-layer detection (price == fair)
# --------------------------------------------------------------------------

def _inner(od: OrderDepth, fair: float) -> Tuple[int, int]:
    """Return (side, volume). side=+1 inner ask at fair, -1 inner bid at fair, 0 none."""
    if od.sell_orders:
        ba = min(od.sell_orders.keys())
        if ba == fair:
            return +1, abs(od.sell_orders[ba])
    if od.buy_orders:
        bb = max(od.buy_orders.keys())
        if bb == fair:
            return -1, abs(od.buy_orders[bb])
    return 0, 0


# --------------------------------------------------------------------------
# Take helpers
# --------------------------------------------------------------------------

def _feasible(q, v, side):
    if side == +1:
        return range(q, min(q + v, POS_LIMIT) + 1)
    if side == -1:
        return range(max(q - v, -POS_LIMIT), q + 1)
    return [q]


def take_choco_vanilla(qC, qV, vC, vV, sideC, sideV, beta=BETA):
    rC = list(_feasible(qC, vC, sideC))
    rV = list(_feasible(qV, vV, sideV))
    best_cost, best = None, (qC, qV)
    for c_new in rC:
        for v_new in rV:
            cost = (c_new - v_new) ** 2 + beta * (c_new ** 2 + v_new ** 2)
            if best_cost is None or cost < best_cost:
                best_cost, best = cost, (c_new, v_new)
    return best[0] - qC, best[1] - qV


def take_strawberry(q, v, side):
    if side == +1:
        return min(v, POS_LIMIT - q)
    return 0


def take_pistachio(q, v, side):
    if side == -1:
        return -min(v, POS_LIMIT + q)
    return 0


def take_raspberry(q, v, side, fair_R, mu):
    # 树莓高于均值：预期回落，因此应该卖
    if fair_R > mu + THRESHOLD and side == -1:
        return -min(v, POS_LIMIT + q)

    # 树莓低于均值：预期反弹，因此应该买
    if fair_R < mu - THRESHOLD and side == +1:
        return min(v, POS_LIMIT - q)

    return 0


# --------------------------------------------------------------------------
# Quote
# --------------------------------------------------------------------------

def quote(q, best_bid, best_ask, fair):
    bid_px = best_bid + 1
    bid_sz = POS_LIMIT - q
    if bid_px >= fair or bid_sz <= 0:
        bid_sz = 0

    ask_px = best_ask - 1
    ask_sz = POS_LIMIT + q
    if ask_px <= fair or ask_sz <= 0:
        ask_sz = 0

    return bid_px, bid_sz, ask_px, ask_sz


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
        mu: float = persistent.get("mu", RASPBERRY_INIT_MU)

        # ---- gather per-product context
        ctx: Dict[str, dict] = {}
        for product in PRODUCTS:
            od = state.order_depths.get(product)
            if od is None:
                continue
            wm = _wall_mid(od, prev_walls.get(product))
            if wm is None:
                continue
            prev_walls[product] = wm
            fair = _fair_price(od, wm)
            side, vol = _inner(od, fair)
            bb = max(od.buy_orders.keys()) if od.buy_orders else None
            ba = min(od.sell_orders.keys()) if od.sell_orders else None
            ctx[product] = {
                "od": od, "fair": fair, "side": side, "vol": vol,
                "bb": bb, "ba": ba,
                "q": state.position.get(product, 0),
            }

        # ---- 1) update RASPBERRY mu
        if RASPBERRY in ctx:
            mu = mu * (1 - ALPHA) + ctx[RASPBERRY]["fair"] * ALPHA

        result: Dict[str, List[Order]] = {p: [] for p in PRODUCTS}

        # ---- 2) take
        # CHOCO/VANILLA pair
        if CHOCOLATE in ctx and VANILLA in ctx:
            cC, cV = ctx[CHOCOLATE], ctx[VANILLA]
            dC, dV = take_choco_vanilla(
                cC["q"], cV["q"], cC["vol"], cV["vol"],
                cC["side"], cV["side"],
            )
            if dC > 0:
                result[CHOCOLATE].append(Order(CHOCOLATE, cC["ba"], dC))
            elif dC < 0:
                result[CHOCOLATE].append(Order(CHOCOLATE, cC["bb"], dC))
            if dV > 0:
                result[VANILLA].append(Order(VANILLA, cV["ba"], dV))
            elif dV < 0:
                result[VANILLA].append(Order(VANILLA, cV["bb"], dV))
            cC["q"] += dC
            cV["q"] += dV

        if STRAWBERRY in ctx:
            s = ctx[STRAWBERRY]
            d = take_strawberry(s["q"], s["vol"], s["side"])
            if d > 0:
                result[STRAWBERRY].append(Order(STRAWBERRY, s["ba"], d))
                s["q"] += d

        if PISTACHIO in ctx:
            s = ctx[PISTACHIO]
            d = take_pistachio(s["q"], s["vol"], s["side"])
            if d < 0:
                result[PISTACHIO].append(Order(PISTACHIO, s["bb"], d))
                s["q"] += d

        if RASPBERRY in ctx:
            s = ctx[RASPBERRY]
            d = take_raspberry(s["q"], s["vol"], s["side"], s["fair"], mu)
            if d > 0:
                result[RASPBERRY].append(Order(RASPBERRY, s["ba"], d))
                s["q"] += d
            elif d < 0:
                result[RASPBERRY].append(Order(RASPBERRY, s["bb"], d))
                s["q"] += d

        # ---- 3) quote
        for product, c in ctx.items():
            if c["bb"] is None or c["ba"] is None:
                continue
            bid_px, bid_sz, ask_px, ask_sz = quote(
                c["q"], c["bb"], c["ba"], c["fair"]
            )
            if bid_sz > 0:
                result[product].append(Order(product, bid_px, bid_sz))
            if ask_sz > 0:
                result[product].append(Order(product, ask_px, -ask_sz))

        result = {p: o for p, o in result.items() if o}
        return result, 0, json.dumps({"walls": prev_walls, "mu": mu})
