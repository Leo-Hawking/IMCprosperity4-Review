"""make_polyunit.py — 45 个非 PEBBLES 资产的独立做市

按 单资产做市策略规范.md 实现：
  1) 内层订单价格 == fair 时，调用 take 把仓位往 0 拉
  2) 调用 quote 下达 ±1 改善价的外层挂单

fair price 由 round5 wall-mid 规则给出（CLAUDE.md）。
"""

import json
from typing import Dict, List, Optional

try:
    from datamodel import Order, OrderDepth, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, OrderDepth, TradingState


PRODUCTS = [
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
    # Snack Packs
    "SNACKPACK_CHOCOLATE", "SNACKPACK_VANILLA", "SNACKPACK_PISTACHIO",
    "SNACKPACK_STRAWBERRY", "SNACKPACK_RASPBERRY",
]

POS_LIMIT = 10


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

        for product in PRODUCTS:
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

            bb = max(od.buy_orders.keys()) if od.buy_orders else None
            ba = min(od.sell_orders.keys()) if od.sell_orders else None

            # ---- 1) take: inner price == fair, pull position toward zero
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

            # ---- 2) quote
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

        return result, 0, json.dumps({"walls": prev_walls})
