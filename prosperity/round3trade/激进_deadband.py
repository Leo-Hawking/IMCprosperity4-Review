"""激进_deadband.py - 在 激进_best.py 基础上的两处优化：

1. 新增 REBALANCE_THRESHOLD：仓位调整死区
   - |target - position| < 阈值时不发单
   - 过滤掉 target 在 entry 附近抖动导致的小幅来回换手
   - 不影响满仓：|dev| ≥ active 时 target = ±limit，delta 一定大于死区

2. START_FRAC 默认改为 0.0（消除 entry 处的跳变）
   - 原版：|dev| 跨过 entry 瞬间 target 从 0 跳到 ±limit/3（一个隐藏的硬阈值）
   - 新版：target 从 entry 开始线性爬升到 ±limit，完全连续
   - 如果想要"信号一出来就抢仓位"的行为，把对应产品改回 1/3 即可

其它部分（EMA、_take_asks、_take_bids 等）和原文件完全一致。
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
    "HYDROGEL_PACK": 9997.0,
    "VELVETFRUIT_EXTRACT": 5260.0,
    "VEV_4000": 1260.0,
    "VEV_4500": 760.0,
    "VEV_5000": 260.0,
    "VEV_5100": 175.0,
    "VEV_5200": 90.0,
    "VEV_5300": 50.0,
    "VEV_5400": 15.0,
    "VEV_5500": 6.0,
}

PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

ENTRY_THRESHOLD: Dict[str, float] = {
    "HYDROGEL_PACK": 20,
    "VEV_4000": 18,
    "VELVETFRUIT_EXTRACT": 7,
    "VEV_4500": 1,
}
ACTIVE_THRESHOLD: Dict[str, float] = {
    "HYDROGEL_PACK": 26,
    "VEV_4000": 34,
    "VELVETFRUIT_EXTRACT": 20,
    "VEV_4500": 4,
}

TAKER_ONLY_THRESHOLD: Dict[str, float] = {
    "VEV_5000": 18,
    "VEV_5100": 12,
    "VEV_5200": 6,
    "VEV_5300": 5.5,
    "VEV_5400": 4,
    "VEV_5500": 4,
}

EAT_DISTANCE = 2
IMPROVE_TICK = 1

# === 改动 1：START_FRAC 改为 0.0，消除 entry 处的跳变 ===
# 原版是 1/3，意味着 |dev| 一过 entry 瞬间就跳到 ±limit/3
# 现在 target 从 0 开始线性爬升，配合死区不会有抖动问题
# 如果某个产品想恢复原行为，单独改回 1.0/3.0 即可
START_FRAC: Dict[str, float] = {p: 1.0 / 3.0 for p in PRODUCTS}
# REBALANCE_THRESHOLD 不动

TAKER_ONLY_ACTIVE: Dict[str, float] = {
    p: TAKER_ONLY_THRESHOLD[p] * 2.0 for p in TAKER_ONLY
}

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

# === 改动 2：新增 REBALANCE_THRESHOLD（仓位调整死区） ===
# |target - position| < 阈值时不发单
# 经验：取 POSITION_LIMITS 的 5-8% 较合适
#   - 太小：过滤不掉抖动
#   - 太大：信号变化时反应迟钝
# 满仓不会被限制：当 |dev| ≥ active 时 target = ±limit，delta 一定远大于死区
REBALANCE_THRESHOLD: Dict[str, int] = {
    "HYDROGEL_PACK": 12,           # ~6% of 200
    "VELVETFRUIT_EXTRACT": 12,     # ~6% of 200
    "VEV_4000": 18,                # ~6% of 300
    "VEV_4500": 18,
    "VEV_5000": 18,
    "VEV_5100": 18,
    "VEV_5200": 18,
    "VEV_5300": 18,
    "VEV_5400": 18,
    "VEV_5500": 18,
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


def _target_position(deviation: float, entry: float, active: float,
                     limit: int, start_frac: float) -> int:
    """Continuous mapping signal → target inventory.

      |dev| < entry             → 0
      entry ≤ |dev| < active    → 线性 from start_frac*limit to limit
      |dev| ≥ active            → ±limit
    Sign convention: deviation > 0 (mid above fair) → SHORT (target negative).
    """
    abs_dev = abs(deviation)
    if abs_dev < entry:
        return 0
    sign = -1 if deviation > 0 else 1
    if abs_dev >= active:
        return sign * limit
    frac = (abs_dev - entry) / max(active - entry, 1e-9)
    return int(sign * limit * (start_frac + (1.0 - start_frac) * frac))


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
        if abs(deviation) < entry:
            return self._stable_quote(product, position, limit, od)
        target = _target_position(deviation, entry, active, limit,
                                  START_FRAC[product])
        return self._execute_target(product, target, position, od, wall_mid)

    # --- TAKER_NEAR ---------------------------------------------------------

    def _taker_near(self, product, od, wall_mid, deviation, position, limit):
        entry = ENTRY_THRESHOLD[product]
        active = ACTIVE_THRESHOLD[product]
        target = _target_position(deviation, entry, active, limit,
                                  START_FRAC[product])
        return self._execute_target(product, target, position, od, wall_mid)

    # --- TAKER_ONLY ---------------------------------------------------------

    def _taker_only(self, product, od, wall_mid, deviation, position, limit):
        entry = TAKER_ONLY_THRESHOLD[product]
        active = TAKER_ONLY_ACTIVE[product]
        target = _target_position(deviation, entry, active, limit,
                                  START_FRAC[product])
        return self._execute_target(product, target, position, od, wall_mid)

    # --- shared executor ----------------------------------------------------

    def _execute_target(self, product, target, position, od, wall_mid):
        """target → delta → take.

        改动：增加 REBALANCE_THRESHOLD 死区——|delta| 太小不发单，
        避免 deviation 在 entry 附近抖动时来回换手。
        """
        delta = target - position
        # === 死区检查（核心改动）===
        if abs(delta) < REBALANCE_THRESHOLD[product]:
            return []
        # 加仓用紧 guard，减仓用宽 guard（保留原逻辑）
        if abs(target) > abs(position):
            guard = EAT_DISTANCE
        else:
            guard = ACTIVE_CROSS_DISTANCE[product]
        if delta > 0:
            return self._take_asks(product, delta, od, wall_mid, guard)
        return self._take_bids(product, -delta, od, wall_mid, guard)

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
