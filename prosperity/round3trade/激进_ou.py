"""激进_ou.py — 在 455764.py 的 bang-bang 结构上加一层 OU vol-adaptive 阈值。

核心改动：
  - 新增 dev_var EMA（在线估 deviation 方差），dev_std = sqrt(dev_var)
  - 所有 entry / active / TAKER_ONLY 阈值 = K_* × dev_std（写死常数全部移除）
  - bang-bang 结构、反向 unwind、guard distance 全部保留不动
  - persistent state 多存一个 dev_vars dict
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

# Per-product price EMA halflife
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

# Deviation variance EMA halflife.
# 比赛窗口仅 10000 tick → halflife 必须远小于窗口，否则 var EMA 全程不收敛。
# 取 ~2000：~3×halflife=6000 tick 收敛到稳态，前 60% 窗口已自适应；
# 比 price halflife (4000) 慢，阈值不会追着信号跑。
DEV_VAR_HALFLIFE: Dict[str, int] = {
    "HYDROGEL_PACK": 2000,
    "VELVETFRUIT_EXTRACT": 2000,
    "VEV_4000": 2000,
    "VEV_4500": 2000,
    "VEV_5000": 2000,
    "VEV_5100": 2000,
    "VEV_5200": 2000,
    "VEV_5300": 2000,
    "VEV_5400": 2000,
    "VEV_5500": 2000,
}

INITIAL_EMA: Dict[str, float] = {
    "HYDROGEL_PACK": 10000.0,
    "VELVETFRUIT_EXTRACT": 5250.0,
    "VEV_4000": 1250.0,
    "VEV_4500": 750.0,
    "VEV_5000": 260.0,
    "VEV_5100": 160.0,
    "VEV_5200": 90.0,
    "VEV_5300": 45.0,
    "VEV_5400": 14.0,
    "VEV_5500": 6.0,
}

# 冷启动用的 var 种子。
# halflife=2000 时，前 ~2000 tick 仍受种子主导 → 种子必须接近真实稳态。
# 反推：稳态 dev_std ≈ 原版 entry / K_ENTRY，初始 var = (稳态 std)²。
# 例：HYDROGEL entry=20, K_ENTRY=1.6 → std≈12.5 → var≈156。
INITIAL_DEV_VAR: Dict[str, float] = {
    "HYDROGEL_PACK": 156.0,        # entry=20, K=1.6 → std=12.5
    "VELVETFRUIT_EXTRACT": 49.0,   # entry=7,  K=1.0 → std=7
    "VEV_4000": 144.0,             # entry=18, K=1.5 → std=12
    "VEV_4500": 1.0,               # entry=1,  K=1.0 → std=1
    "VEV_5000": 144.0,             # thr=18,   K=1.5 → std=12
    "VEV_5100": 64.0,              # thr=12,   K=1.5 → std=8
    "VEV_5200": 16.0,              # thr=6,    K=1.5 → std=4
    "VEV_5300": 16.0,              # thr=5.5,  K=1.4 → std≈4
    "VEV_5400": 9.0,               # thr=4,    K=1.3 → std≈3
    "VEV_5500": 9.0,               # thr=4,    K=1.3 → std≈3
}

PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

# 阈值 = K × dev_std。
# 初值大致对应原版常数阈值除以 sqrt(initial_var)，先跑一遍再 per-product 搜。
K_ENTRY: Dict[str, float] = {
    "HYDROGEL_PACK": 1.2,
    "VEV_4000": 1.875,
    "VELVETFRUIT_EXTRACT": 0.75,
    "VEV_4500": 0.75,
}
K_ACTIVE: Dict[str, float] = {
    "HYDROGEL_PACK": 1.365,
    "VEV_4000": 2.1,
    "VELVETFRUIT_EXTRACT": 1.54,
    "VEV_4500": 3.0,
}
K_TAKER_ONLY: Dict[str, float] = {
    "VEV_5000": 1.5,
    "VEV_5100": 1.05,
    "VEV_5200": 0.825,
    "VEV_5300": 1.19,
    "VEV_5400": 0.91,
    "VEV_5500": 0.91,
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
    "VEV_5200": 1.5,
    "VEV_5300": 1.5,
    "VEV_5400": 0.75,
    "VEV_5500": 0.75,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alpha(halflife: int) -> float:
    return 1.0 - math.exp(-math.log(2.0) / halflife)


_ALPHA: Dict[str, float] = {p: _alpha(hl) for p, hl in HALFLIFE.items()}
_ALPHA_VAR: Dict[str, float] = {p: _alpha(hl) for p, hl in DEV_VAR_HALFLIFE.items()}


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
        dev_vars: Dict[str, float] = persistent.get("dev_vars", {})

        result: Dict[str, List[Order]] = {}

        for product in PRODUCTS:
            od = state.order_depths.get(product)
            if od is None:
                continue
            wall_mid = _wall_mid(od)
            if wall_mid is None:
                continue

            # 价格 EMA
            prev_ema = emas.get(product, INITIAL_EMA[product])
            a = _ALPHA[product]
            ema = a * wall_mid + (1.0 - a) * prev_ema
            emas[product] = ema

            deviation = wall_mid - ema

            # Deviation 方差 EMA → 自适应尺度
            prev_var = dev_vars.get(product, INITIAL_DEV_VAR[product])
            a_var = _ALPHA_VAR[product]
            dev_var = a_var * (deviation ** 2) + (1.0 - a_var) * prev_var
            dev_vars[product] = dev_var
            dev_std = math.sqrt(max(dev_var, 1e-9))

            position = state.position.get(product, 0)
            limit = POSITION_LIMITS[product]

            if product in PASSIVE_QUOTE:
                orders = self._passive_quote(product, od, wall_mid,
                                             deviation, position, limit, dev_std)
            elif product in TAKER_NEAR:
                orders = self._taker_near(product, od, wall_mid,
                                          deviation, position, limit, dev_std)
            else:
                orders = self._taker_only(product, od, wall_mid,
                                          deviation, position, limit, dev_std)

            if orders:
                result[product] = orders

        return result, 0, json.dumps({"emas": emas, "dev_vars": dev_vars})

    # --- PASSIVE_QUOTE ------------------------------------------------------

    def _passive_quote(self, product, od, wall_mid, deviation, position, limit, dev_std):
        entry = K_ENTRY[product] * dev_std
        active = K_ACTIVE[product] * dev_std
        abs_dev = abs(deviation)

        # 反向仓位：mid 已回到/穿越 ema，立刻 unwind。
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

        # entry ≤ |dev| < active：紧守门吃 + 剩余挂被动单。
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

    def _taker_near(self, product, od, wall_mid, deviation, position, limit, dev_std):
        entry = K_ENTRY[product] * dev_std
        active = K_ACTIVE[product] * dev_std
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

    def _taker_only(self, product, od, wall_mid, deviation, position, limit, dev_std):
        thr = K_TAKER_ONLY[product] * dev_std
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
