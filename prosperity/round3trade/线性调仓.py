"""EMA deviation -> target-position strategy.

核心逻辑：
1. 用 wall_mid 更新 per-product EMA。
2. 根据 deviation = wall_mid - ema 线性映射目标仓位。
   - deviation > 0: 当前价格高于 EMA，目标偏空。
   - deviation < 0: 当前价格低于 EMA，目标偏多。
3. 只有 abs(target_position - current_position) 达到调仓阈值时才下单。
4. 单次下单方向和数量由 delta = target_position - current_position 决定。
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

# Per-product EMA halflife
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

# Target-position config:
#   target = -limit * clip(deviation / full_deviation, -1, 1)
#   deviation > 0  → price above EMA → target short
#   deviation < 0  → price below EMA → target long
#
# full_deviation 表示偏离 EMA 多少时打满仓。
# 这里默认复用原来的 active threshold，让风险尺度和原策略接近。
TARGET_FULL_DEVIATION: Dict[str, float] = {
    "HYDROGEL_PACK": ACTIVE_THRESHOLD["HYDROGEL_PACK"],
    "VEV_4000": ACTIVE_THRESHOLD["VEV_4000"],
    "VELVETFRUIT_EXTRACT": ACTIVE_THRESHOLD["VELVETFRUIT_EXTRACT"],
    "VEV_4500": ACTIVE_THRESHOLD["VEV_4500"],
    "VEV_5000": TAKER_ONLY_THRESHOLD["VEV_5000"] * 2.0,
    "VEV_5100": TAKER_ONLY_THRESHOLD["VEV_5100"] * 2.0,
    "VEV_5200": TAKER_ONLY_THRESHOLD["VEV_5200"] * 2.0,
    "VEV_5300": TAKER_ONLY_THRESHOLD["VEV_5300"] * 2.0,
    "VEV_5400": TAKER_ONLY_THRESHOLD["VEV_5400"] * 2.0,
    "VEV_5500": TAKER_ONLY_THRESHOLD["VEV_5500"] * 2.0,
}

# 调仓死区：如果 abs(target - position) 小于这个值，则不下单。
# 默认是限仓的 5%，可以作为后续搜索参数。
REBALANCE_THRESHOLD: Dict[str, int] = {
    p: max(1, int(POSITION_LIMITS[p] * 0.05)) for p in PRODUCTS
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


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _target_position(deviation: float, full_deviation: float, limit: int) -> int:
    """Map EMA deviation to target inventory.

    Linear clipped mapping:
      deviation = 0                → target = 0
      deviation = +full_deviation  → target = -limit
      deviation = -full_deviation  → target = +limit

    Sign convention:
      deviation > 0 means mid is above EMA, so inventory leans short.
      deviation < 0 means mid is below EMA, so inventory leans long.
    """
    if full_deviation <= 0:
        return 0

    scaled = _clip(deviation / full_deviation, -1.0, 1.0)
    return int(round(-limit * scaled))


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

            target = _target_position(
                deviation=deviation,
                full_deviation=TARGET_FULL_DEVIATION[product],
                limit=limit,
            )

            orders = self._execute_target(
                product=product,
                target=target,
                position=position,
                od=od,
                wall_mid=wall_mid,
            )

            if orders:
                result[product] = orders

        return result, 0, json.dumps({"emas": emas})

    # --- shared executor ----------------------------------------------------

    def _execute_target(self, product, target, position, od, wall_mid):
        """target → delta → order.

        delta = target - current_position

        如果 abs(delta) 小于调仓阈值，则不交易。
        否则，提交订单数量等于完整仓位差值。
        注意：实际成交可能小于提交数量，取决于盘口和撮合。
        """
        delta = target - position

        if abs(delta) < REBALANCE_THRESHOLD[product]:
            return []

        # 加仓时更保守；减仓或反向时允许吃更宽一点，控制库存风险。
        reducing_or_flipping = (
            (position != 0 and target * position <= 0)
            or (abs(target) < abs(position))
        )

        guard = ACTIVE_CROSS_DISTANCE[product] if reducing_or_flipping else EAT_DISTANCE

        if delta > 0:
            return self._take_asks(
                product=product,
                qty=delta,
                od=od,
                wall_mid=wall_mid,
                max_diff=guard,
            )

        return self._take_bids(
            product=product,
            qty=-delta,
            od=od,
            wall_mid=wall_mid,
            max_diff=guard,
        )

    # --- order primitives ---------------------------------------------------

    @staticmethod
    def _take_asks(product, qty, od, wall_mid, max_diff):
        """Buy qty to move current position toward target.

        提交订单数量等于 qty。
        价格选择 max_diff 内最激进的 ask，因此会扫掉这个价格以内的卖单。
        """
        if qty <= 0 or not od.sell_orders:
            return []

        acceptable_asks = [
            ask for ask in od.sell_orders
            if ask - wall_mid < max_diff
        ]

        if not acceptable_asks:
            return []

        px = max(acceptable_asks)
        return [Order(product, px, qty)]

    @staticmethod
    def _take_bids(product, qty, od, wall_mid, max_diff):
        """Sell qty to move current position toward target.

        提交订单数量等于 qty。
        价格选择 max_diff 内最激进的 bid，因此会扫掉这个价格以内的买单。
        """
        if qty <= 0 or not od.buy_orders:
            return []

        acceptable_bids = [
            bid for bid in od.buy_orders
            if wall_mid - bid < max_diff
        ]

        if not acceptable_bids:
            return []

        px = min(acceptable_bids)
        return [Order(product, px, -qty)]

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