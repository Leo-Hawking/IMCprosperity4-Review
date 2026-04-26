"""激进.py 的搜索版：HALFLIFE 改成 per-product dict，其它和原文件一致。

仅这一处改动允许我们在 backtest/search.py 里只 patch 单产品的 halflife
而不影响其它产品 → 实现"每个资产相互独立"的超参数搜索。
"""

import json
import math
from typing import Dict, List

try:
    from datamodel import Order, OrderDepth, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, OrderDepth, TradingState


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PRODUCTS = [
    "HYDROGEL_PACK",
    "VELVETFRUIT_EXTRACT",
    "VEV_4000",
    "VEV_4500",
    "VEV_5000",
    "VEV_5100",
    "VEV_5200",
    "VEV_5300",
    "VEV_5400",
    "VEV_5500",
]

POSITION_LIMITS: Dict[str, int] = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300,
    "VEV_4500": 300,
    "VEV_5000": 300,
    "VEV_5100": 300,
    "VEV_5200": 300,
    "VEV_5300": 300,
    "VEV_5400": 300,
    "VEV_5500": 300,
}

# Per-product EMA halflife（从单一全局 4000 改成 dict, 默认值仍为 4000）。
HALFLIFE: Dict[str, int] = {
    "HYDROGEL_PACK": 4000,
    "VELVETFRUIT_EXTRACT": 4000,
    "VEV_4000": 4000,
    "VEV_4500": 4000,
    "VEV_5000": 4000,
    "VEV_5100": 4000,
    "VEV_5200": 4000,
    "VEV_5300": 4000,
    "VEV_5400": 4000,
    "VEV_5500": 4000,
}

INITIAL_EMA: Dict[str, float] = {
    "HYDROGEL_PACK": 10000.0,
    "VELVETFRUIT_EXTRACT": 5250.0,
    "VEV_4000": 1250.0,
    "VEV_4500": 750.0,
    "VEV_5000": 250.0,
    "VEV_5100": 165.0,
    "VEV_5200": 90.0,
    "VEV_5300": 45.0,
    "VEV_5400": 30.0,
    "VEV_5500": 20.0,
}

PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

ENTRY_THRESHOLD: Dict[str, float] = {
    "HYDROGEL_PACK": 20.0,
    "VEV_4000": 15.0,
    "VELVETFRUIT_EXTRACT": 10.0,
    "VEV_4500": 10.0,
}
ACTIVE_THRESHOLD: Dict[str, float] = {
    "HYDROGEL_PACK": 30.0,
    "VEV_4000": 30.0,
    "VELVETFRUIT_EXTRACT": 20.0,
    "VEV_4500": 19.0,
}

TAKER_ONLY_THRESHOLD: Dict[str, float] = {
    "VEV_5000": 18.0,
    "VEV_5100": 17.0,
    "VEV_5200": 15.0,
    "VEV_5300": 8.0,
    "VEV_5400": 4.0,
    "VEV_5500": 4.0,
}

EAT_DISTANCE = 2
IMPROVE_TICK = 1

ACTIVE_CROSS_DISTANCE: Dict[str, float] = {
    "HYDROGEL_PACK": 8.0,
    "VEV_4000": 10.5,
    "VELVETFRUIT_EXTRACT": 3.0,
    "VEV_4500": 5.0,
    "VEV_5000": 4.0,
    "VEV_5100": 2.5,
    "VEV_5200": 2,
    "VEV_5300": 1.0,
    "VEV_5400": 1.0,
    "VEV_5500": 1.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alpha(halflife: int) -> float:
    return 1.0 - math.exp(-math.log(2.0) / halflife)


_ALPHA: Dict[str, float] = {p: _alpha(hl) for p, hl in HALFLIFE.items()}


def _wall_mid(od: OrderDepth):
    if not od.buy_orders or not od.sell_orders:
        return None
    bid_wall = max(od.buy_orders.keys(), key=lambda p: (od.buy_orders[p], p))
    ask_wall = min(od.sell_orders.keys(), key=lambda p: (od.sell_orders[p], p))
    return (bid_wall + ask_wall) / 2.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class Trader:
    def run(self, state: TradingState):
        if state.traderData:
            try:
                persistent = json.loads(state.traderData)
            except Exception:
                persistent = {}
        else:
            persistent = {}
        emas: Dict[str, float] = persistent.get("emas", {})

        result: Dict[str, List[Order]] = {}

        for product in PRODUCTS:
            od = state.order_depths.get(product)
            if od is None:
                continue
            wall_mid = _wall_mid(od)
            if wall_mid is None:
                continue

            prev = emas.get(product, INITIAL_EMA[product])
            a = _ALPHA[product]
            ema = a * wall_mid + (1.0 - a) * prev
            emas[product] = ema

            deviation = wall_mid - ema
            position = state.position.get(product, 0)
            limit = POSITION_LIMITS[product]

            if product in PASSIVE_QUOTE:
                orders = self._passive_quote(product, od, wall_mid,
                                             deviation, position, limit)
            elif product in TAKER_NEAR:
                orders = self._taker_near(product, od, wall_mid,
                                          deviation, position, limit)
            else:
                orders = self._taker_only(product, od, wall_mid,
                                          deviation, position, limit)

            if orders:
                result[product] = orders

        return result, 0, json.dumps({"emas": emas})

    # --- PASSIVE_QUOTE ------------------------------------------------------

    def _passive_quote(self, product, od, wall_mid, deviation, position, limit):
        entry = ENTRY_THRESHOLD[product]
        active = ACTIVE_THRESHOLD[product]
        abs_dev = abs(deviation)

        if position > 0 and deviation >= 0:
            return self._take_bids(product, position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])
        if position < 0 and deviation <= 0:
            return self._take_asks(product, -position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])

        if abs_dev < entry:
            return self._stable_quote(product, position, limit, od)

        if abs_dev >= active:
            if deviation < 0:
                qty = limit - position
                return self._take_asks(product, qty, od, wall_mid,
                                       ACTIVE_CROSS_DISTANCE[product])
            else:
                qty = limit + position
                return self._take_bids(product, qty, od, wall_mid,
                                       ACTIVE_CROSS_DISTANCE[product])

        if deviation < 0:
            qty = limit - position
            orders = self._take_asks(product, qty, od, wall_mid, EAT_DISTANCE)
            eaten = sum(o.quantity for o in orders)
            rest = qty - eaten
            if rest > 0:
                orders += self._passive_one_side(product, rest, od, side=+1)
            return orders
        else:
            qty = limit + position
            orders = self._take_bids(product, qty, od, wall_mid, EAT_DISTANCE)
            eaten = -sum(o.quantity for o in orders)
            rest = qty - eaten
            if rest > 0:
                orders += self._passive_one_side(product, rest, od, side=-1)
            return orders

    # --- TAKER_NEAR ---------------------------------------------------------

    def _taker_near(self, product, od, wall_mid, deviation, position, limit):
        entry = ENTRY_THRESHOLD[product]
        active = ACTIVE_THRESHOLD[product]
        abs_dev = abs(deviation)

        if position > 0 and deviation >= 0:
            return self._take_bids(product, position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])
        if position < 0 and deviation <= 0:
            return self._take_asks(product, -position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])

        if abs_dev < entry:
            return []

        guard = ACTIVE_CROSS_DISTANCE[product] if abs_dev >= active else EAT_DISTANCE

        if deviation < 0:
            qty = limit - position
            return self._take_asks(product, qty, od, wall_mid, guard)
        else:
            qty = limit + position
            return self._take_bids(product, qty, od, wall_mid, guard)

    # --- TAKER_ONLY ---------------------------------------------------------

    def _taker_only(self, product, od, wall_mid, deviation, position, limit):
        thr = TAKER_ONLY_THRESHOLD[product]
        guard = ACTIVE_CROSS_DISTANCE[product]
        abs_dev = abs(deviation)

        if position > 0 and deviation >= 0:
            return self._take_bids(product, position, od, wall_mid, guard)
        if position < 0 and deviation <= 0:
            return self._take_asks(product, -position, od, wall_mid, guard)

        if abs_dev < thr:
            return []

        if deviation < 0:
            qty = limit - position
            return self._take_asks(product, qty, od, wall_mid, guard)
        else:
            qty = limit + position
            return self._take_bids(product, qty, od, wall_mid, guard)

    # --- order primitives ---------------------------------------------------

    @staticmethod
    def _take_asks(product, qty, od, wall_mid, max_diff):
        if qty <= 0 or not od.sell_orders:
            return []
        orders: List[Order] = []
        remaining = qty
        for ask in sorted(od.sell_orders.keys()):
            if ask - wall_mid >= max_diff:
                break
            vol = -od.sell_orders[ask]
            take = min(remaining, vol)
            if take > 0:
                orders.append(Order(product, ask, take))
                remaining -= take
            if remaining <= 0:
                break
        return orders

    @staticmethod
    def _take_bids(product, qty, od, wall_mid, max_diff):
        if qty <= 0 or not od.buy_orders:
            return []
        orders: List[Order] = []
        remaining = qty
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if wall_mid - bid >= max_diff:
                break
            vol = od.buy_orders[bid]
            take = min(remaining, vol)
            if take > 0:
                orders.append(Order(product, bid, -take))
                remaining -= take
            if remaining <= 0:
                break
        return orders

    @staticmethod
    def _passive_one_side(product, qty, od, side):
        if qty <= 0:
            return []
        if side > 0:
            if not od.buy_orders:
                return []
            px = max(od.buy_orders.keys()) + IMPROVE_TICK
            if od.sell_orders and px >= min(od.sell_orders.keys()):
                return []
            return [Order(product, px, qty)]
        else:
            if not od.sell_orders:
                return []
            px = min(od.sell_orders.keys()) - IMPROVE_TICK
            if od.buy_orders and px <= max(od.buy_orders.keys()):
                return []
            return [Order(product, px, -qty)]

    @staticmethod
    def _stable_quote(product, position, limit, od):
        if not od.buy_orders or not od.sell_orders:
            return []
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        buy_px = best_bid + IMPROVE_TICK
        sell_px = best_ask - IMPROVE_TICK
        if buy_px >= best_ask or sell_px <= best_bid:
            return []
        orders: List[Order] = []
        buy_qty = limit - position
        sell_qty = limit + position
        if buy_qty > 0:
            orders.append(Order(product, buy_px, buy_qty))
        if sell_qty > 0:
            orders.append(Order(product, sell_px, -sell_qty))
        return orders
