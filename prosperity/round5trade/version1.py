"""version1.py - combined round5 strategy.

Combines:
  - PEBBLES_XS/S/M/L/XL coupled market making and hedging.
  - 45 non-PEBBLES assets independent single-asset market making.
"""

import json
from typing import Dict, List, Optional, Tuple

try:
    from datamodel import Order, OrderDepth, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, OrderDepth, TradingState


PEBBLES_PRODUCTS = ["PEBBLES_XS", "PEBBLES_S", "PEBBLES_M", "PEBBLES_L", "PEBBLES_XL"]

POLYUNIT_PRODUCTS = [
    # Galaxy Sounds
    "GALAXY_SOUNDS_DARK_MATTER", "GALAXY_SOUNDS_BLACK_HOLES",
    "GALAXY_SOUNDS_PLANETARY_RINGS", "GALAXY_SOUNDS_SOLAR_WINDS",
    "GALAXY_SOUNDS_SOLAR_FLAMES",
    # Sleep Pods
    "SLEEP_POD_SUEDE", "SLEEP_POD_LAMB_WOOL", "SLEEP_POD_POLYESTER",
    "SLEEP_POD_NYLON", "SLEEP_POD_COTTON",
    # Microchips
    "MICROCHIP_CIRCLE", "MICROCHIP_OVAL", "MICROCHIP_SQUARE",
    "MICROCHIP_RECTANGLE", "MICROCHIP_TRIANGLE",
    # Robots
    "ROBOT_VACUUMING", "ROBOT_MOPPING", "ROBOT_DISHES",
    "ROBOT_LAUNDRY", "ROBOT_IRONING",
    # UV Visors
    "UV_VISOR_YELLOW", "UV_VISOR_AMBER", "UV_VISOR_ORANGE",
    "UV_VISOR_RED", "UV_VISOR_MAGENTA",
    # Translators
    "TRANSLATOR_SPACE_GRAY", "TRANSLATOR_ASTRO_BLACK",
    "TRANSLATOR_ECLIPSE_CHARCOAL", "TRANSLATOR_GRAPHITE_MIST",
    "TRANSLATOR_VOID_BLUE",
    # Panels
    "PANEL_1X2", "PANEL_2X2", "PANEL_1X4", "PANEL_2X4", "PANEL_4X4",
    # Oxygen Shakes
    "OXYGEN_SHAKE_MORNING_BREATH", "OXYGEN_SHAKE_EVENING_BREATH",
    "OXYGEN_SHAKE_MINT", "OXYGEN_SHAKE_CHOCOLATE", "OXYGEN_SHAKE_GARLIC",
]

CHOCOLATE = "SNACKPACK_CHOCOLATE"
VANILLA = "SNACKPACK_VANILLA"
PISTACHIO = "SNACKPACK_PISTACHIO"
STRAWBERRY = "SNACKPACK_STRAWBERRY"
RASPBERRY = "SNACKPACK_RASPBERRY"
SNACK_PRODUCTS = [CHOCOLATE, VANILLA, PISTACHIO, STRAWBERRY, RASPBERRY]

POS_LIMIT = 10
BETA = 0.1

SPAN = 4000
ALPHA = 2.0 / (SPAN + 1)
THRESHOLD = 140
RASPBERRY_INIT_MU = 10000.0

SPAN_SHORT = 800
ALPHA_SHORT = 2.0 / (SPAN_SHORT + 1)
THR_SHORT = 16
STRAWBERRY_INIT_MU = 10300.0
PISTACHIO_INIT_MU = 9650.0


# --------------------------------------------------------------------------
# Fair price (round5 wall-mid rule)
# --------------------------------------------------------------------------

def _max_vol_price(levels: Dict[int, int]) -> Optional[int]:
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


def _best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bb = max(od.buy_orders.keys()) if od.buy_orders else None
    ba = min(od.sell_orders.keys()) if od.sell_orders else None
    return bb, ba


# --------------------------------------------------------------------------
# PEBBLES strategy
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
        risk_sq = (
            (a_new - e_new) ** 2
            + (b_new - e_new) ** 2
            + (c_new - e_new) ** 2
            + (d_new - e_new) ** 2
        )
        cost = risk_sq + beta * abs(e_new)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best = (a_new, b_new, c_new, d_new, e_new)

    a_new, b_new, c_new, d_new, e_new = best
    return (a_new - a, b_new - b, c_new - c, d_new - d, e_new - e)


def _inner_direction(od: OrderDepth, fair: float) -> Tuple[int, int, int, int]:
    """Return (side, take_volume_available, best_bid, best_ask)."""
    bb, ba = _best_bid_ask(od)
    if bb is None or ba is None:
        return 0, 0, bb if bb is not None else 0, ba if ba is not None else 0

    if ba < fair:
        return +1, abs(od.sell_orders[ba]), bb, ba
    if bb > fair:
        return -1, abs(od.buy_orders[bb]), bb, ba
    return 0, 0, bb, ba


def _run_pebbles(state: TradingState, prev_walls: Dict[str, float]) -> Dict[str, List[Order]]:
    positions = []
    fairs = []
    best_bids = []
    best_asks = []
    sides = []
    avail_vols = []
    ods: List[Optional[OrderDepth]] = []

    for product in PEBBLES_PRODUCTS:
        od = state.order_depths.get(product)
        ods.append(od)
        positions.append(state.position.get(product, 0))

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
        side, vol, bb, ba = _inner_direction(od, fair)
        fairs.append(fair)
        best_bids.append(bb)
        best_asks.append(ba)
        sides.append(side)
        avail_vols.append(vol)

    result: Dict[str, List[Order]] = {p: [] for p in PEBBLES_PRODUCTS}

    # 1) take only if all five legs share a favorable inner direction.
    all_have_book = all(od is not None and f is not None for od, f in zip(ods, fairs))
    if all_have_book and sides[0] != 0 and all(s == sides[0] for s in sides):
        side = sides[0]
        v = min(avail_vols)
        if side == +1:
            v = min(v, *(POS_LIMIT - p for p in positions))
        else:
            v = min(v, *(POS_LIMIT + p for p in positions))

        if v > 0:
            deltas = pebbles_take(tuple(positions), v, side)
            for product, delta, bb, ba in zip(PEBBLES_PRODUCTS, deltas, best_bids, best_asks):
                if delta > 0:
                    result[product].append(Order(product, ba, delta))
                elif delta < 0:
                    result[product].append(Order(product, bb, delta))
            positions = [p + d for p, d in zip(positions, deltas)]

    # 2) quote with projected positions after take.
    quote_ok = all(bb is not None and ba is not None and f is not None
                   for bb, ba, f in zip(best_bids, best_asks, fairs))
    if quote_ok:
        quotes = pebbles_quote(positions, best_bids, best_asks, fairs)
        for product, (bid_px, bid_sz, ask_px, ask_sz) in zip(PEBBLES_PRODUCTS, quotes):
            if bid_sz > 0:
                result[product].append(Order(product, bid_px, bid_sz))
            if ask_sz > 0:
                result[product].append(Order(product, ask_px, -ask_sz))

    return {p: orders for p, orders in result.items() if orders}


# --------------------------------------------------------------------------
# Independent single-asset strategy
# --------------------------------------------------------------------------

def _run_polyunits(state: TradingState, prev_walls: Dict[str, float]) -> Dict[str, List[Order]]:
    result: Dict[str, List[Order]] = {}

    for product in POLYUNIT_PRODUCTS:
        od = state.order_depths.get(product)
        if od is None:
            continue

        wm = _wall_mid(od, prev_walls.get(product))
        if wm is None:
            continue
        prev_walls[product] = wm
        fair = _fair_price(od, wm)

        q = state.position.get(product, 0)
        orders: List[Order] = []
        bb, ba = _best_bid_ask(od)

        # 1) take at fair to pull inventory toward zero.
        if q > 0 and bb is not None and bb == fair:
            vol = abs(od.buy_orders[bb])
            qty = min(q, vol)
            if qty > 0:
                orders.append(Order(product, bb, -qty))
                q -= qty
        elif q < 0 and ba is not None and ba == fair:
            vol = abs(od.sell_orders[ba])
            qty = min(-q, vol)
            if qty > 0:
                orders.append(Order(product, ba, qty))
                q += qty

        # 2) quote around fair.
        if bb is not None and ba is not None:
            bid_px = bb + 1
            bid_sz = POS_LIMIT - q
            if bid_px >= fair or bid_sz <= 0:
                bid_sz = 0

            ask_px = ba - 1
            ask_sz = POS_LIMIT + q
            if ask_px <= fair or ask_sz <= 0:
                ask_sz = 0

            if bid_sz > 0:
                orders.append(Order(product, bid_px, bid_sz))
            if ask_sz > 0:
                orders.append(Order(product, ask_px, -ask_sz))

        if orders:
            result[product] = orders

    return result


# --------------------------------------------------------------------------
# Snack Packs strategy
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


def take_strawberry(q, v, side, fair, mu):
    if fair > mu + THR_SHORT and side == -1:
        return -min(v, POS_LIMIT + q)
    if side == +1:
        return min(v, POS_LIMIT - q)
    return 0


def take_pistachio(q, v, side, fair, mu):
    if fair < mu - THR_SHORT and side == +1:
        return min(v, POS_LIMIT - q)
    if side == -1:
        return -min(v, POS_LIMIT + q)
    return 0


def take_raspberry(q, v, side, fair_R, mu):
    if fair_R > mu + THRESHOLD and side == -1:
        return -min(v, POS_LIMIT + q)
    if fair_R < mu - THRESHOLD and side == +1:
        return min(v, POS_LIMIT - q)
    return 0


def snack_quote(q, best_bid, best_ask, fair):
    bid_px = best_bid + 1
    bid_sz = POS_LIMIT - q
    if bid_px >= fair or bid_sz <= 0:
        bid_sz = 0

    ask_px = best_ask - 1
    ask_sz = POS_LIMIT + q
    if ask_px <= fair or ask_sz <= 0:
        ask_sz = 0

    return bid_px, bid_sz, ask_px, ask_sz


def _run_snacks(state: TradingState, prev_walls: Dict[str, float], persistent: dict) -> Tuple[Dict[str, List[Order]], float, float, float]:
    mu: float = persistent.get("mu", RASPBERRY_INIT_MU)
    mu_S: float = persistent.get("mu_S", STRAWBERRY_INIT_MU)
    mu_P: float = persistent.get("mu_P", PISTACHIO_INIT_MU)

    ctx: Dict[str, dict] = {}
    for product in SNACK_PRODUCTS:
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

    if RASPBERRY in ctx:
        mu = mu * (1 - ALPHA) + ctx[RASPBERRY]["fair"] * ALPHA
    if STRAWBERRY in ctx:
        mu_S = mu_S * (1 - ALPHA_SHORT) + ctx[STRAWBERRY]["fair"] * ALPHA_SHORT
    if PISTACHIO in ctx:
        mu_P = mu_P * (1 - ALPHA_SHORT) + ctx[PISTACHIO]["fair"] * ALPHA_SHORT

    result: Dict[str, List[Order]] = {p: [] for p in SNACK_PRODUCTS}

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
        d = take_strawberry(s["q"], s["vol"], s["side"], s["fair"], mu_S)
        if d > 0:
            result[STRAWBERRY].append(Order(STRAWBERRY, s["ba"], d))
            s["q"] += d
        elif d < 0:
            result[STRAWBERRY].append(Order(STRAWBERRY, s["bb"], d))
            s["q"] += d

    if PISTACHIO in ctx:
        s = ctx[PISTACHIO]
        d = take_pistachio(s["q"], s["vol"], s["side"], s["fair"], mu_P)
        if d > 0:
            result[PISTACHIO].append(Order(PISTACHIO, s["ba"], d))
            s["q"] += d
        elif d < 0:
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

    for product, c in ctx.items():
        if c["bb"] is None or c["ba"] is None:
            continue
        bid_px, bid_sz, ask_px, ask_sz = snack_quote(
            c["q"], c["bb"], c["ba"], c["fair"]
        )
        if bid_sz > 0:
            result[product].append(Order(product, bid_px, bid_sz))
        if ask_sz > 0:
            result[product].append(Order(product, ask_px, -ask_sz))

    return {p: o for p, o in result.items() if o}, mu, mu_S, mu_P


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

        result: Dict[str, List[Order]] = {}
        result.update(_run_pebbles(state, prev_walls))
        result.update(_run_polyunits(state, prev_walls))
        snack_result, mu, mu_S, mu_P = _run_snacks(state, prev_walls, persistent)
        result.update(snack_result)

        return result, 0, json.dumps({"walls": prev_walls, "mu": mu,
                                       "mu_S": mu_S, "mu_P": mu_P})
