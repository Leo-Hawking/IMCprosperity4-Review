"""round4follow.py — round3final.py + 跟单层

在原有的 EMA 触发基础上，叠加同方向跟单：
  - HYDROGEL_PACK：跟 "Mark 14" 在 HYDROGEL_PACK 的成交方向
  - 其余 9 个产品：统一跟 "Mark 55" 在 VELVETFRUIT_EXTRACT 的成交方向

跟单触发的必要条件：
  1. 该产品自身 EMA 处于触发区（|deviation| ≥ entry / threshold）
  2. 上一 tick 的市场成交里目标 Mark 在监测产品上有净方向（buy/sell）
  3. 该方向与本产品 EMA 偏离方向一致（deviation < 0 → 跟买；deviation > 0 → 跟卖）

跟单只产生额外的 take 订单（穿对手价吃单），用 ACTIVE_CROSS_DISTANCE 作为护栏，
不替换原 EMA 订单 — 两路订单合并下发，剩余仓位空间共享。
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

HALFLIFE: Dict[str, int] = {p: 4000 for p in PRODUCTS}

INITIAL_EMA: Dict[str, float] = {
    "HYDROGEL_PACK": 9980.0,
    "VELVETFRUIT_EXTRACT": 5250.0,
    "VEV_4000": 1250.0,
    "VEV_4500": 750.0,
    "VEV_5000": 260.0,
    "VEV_5100": 160.0,
    "VEV_5200": 85.0,
    "VEV_5300": 35.0,
    "VEV_5400": 10.0,
    "VEV_5500": 4.0,
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

ACTIVE_CROSS_DISTANCE: Dict[str, float] = {
    "HYDROGEL_PACK": 8.5,
    "VEV_4000": 11,
    "VELVETFRUIT_EXTRACT": 3,
    "VEV_4500": 5.0,
    "VEV_5000": 3.5,
    "VEV_5100": 2.5,
    "VEV_5200": 1.5,
    "VEV_5300": 1.0,
    "VEV_5400": 0.75,
    "VEV_5500": 0.75,
}

# ---- 跟单配置 ----
# 每个产品监测哪个 Mark 的哪个产品成交。
# 默认：HYDROGEL 跟 Mark 14 自身；其他 9 个跟 Mark 55 在 VELVET 的方向。
FOLLOW_MARK: Dict[str, str] = {
    "HYDROGEL_PACK":       "Mark 14",
    "VELVETFRUIT_EXTRACT": "Mark 55",
    "VEV_4000":            "Mark 55",
    "VEV_4500":            "Mark 55",
    "VEV_5000":            "Mark 55",
    "VEV_5100":            "Mark 55",
    "VEV_5200":            "Mark 55",
    "VEV_5300":            "Mark 55",
    "VEV_5400":            "Mark 55",
    "VEV_5500":            "Mark 55",
}
FOLLOW_SOURCE: Dict[str, str] = {
    "HYDROGEL_PACK":       "HYDROGEL_PACK",
    "VELVETFRUIT_EXTRACT": "VELVETFRUIT_EXTRACT",
    "VEV_4000":            "VELVETFRUIT_EXTRACT",
    "VEV_4500":            "VELVETFRUIT_EXTRACT",
    "VEV_5000":            "VELVETFRUIT_EXTRACT",
    "VEV_5100":            "VELVETFRUIT_EXTRACT",
    "VEV_5200":            "VELVETFRUIT_EXTRACT",
    "VEV_5300":            "VELVETFRUIT_EXTRACT",
    "VEV_5400":            "VELVETFRUIT_EXTRACT",
    "VEV_5500":            "VELVETFRUIT_EXTRACT",
}
# 单次跟单的最大数量（占该产品 limit 的比例上限），避免跟单一笔吃满全部仓位
FOLLOW_MAX_FRAC: Dict[str, float] = {p: 1.0 for p in PRODUCTS}

# 跟单起始计数：在 EMA 触发区内，第 N 次"方向一致的 Mark 净成交"才开始跟单。
# 默认 N=2，即跳过第 1 个信号，从第 2 个起跟单。设为 1 则恢复"逢号必跟"。
# EMA 离开触发区（|deviation| < entry threshold）时计数器清零。
FOLLOW_START_COUNT: Dict[str, int] = {p: 2 for p in PRODUCTS}


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


def _mark_net_direction(market_trades, symbol: str, mark: str) -> int:
    """net qty signed: + = mark net bought, - = mark net sold, 0 = no trade."""
    trades = market_trades.get(symbol) or []
    net = 0
    for t in trades:
        if t.buyer == mark:
            net += t.quantity
        if t.seller == mark:
            net -= t.quantity
    return net


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
        follow_counts: Dict[str, int] = persistent.get("follow_counts", {})

        # Pre-compute mark net directions (cached so identical FOLLOW_SOURCE
        # for 9 products doesn't recompute).
        mark_dir_cache: Dict[tuple, int] = {}

        def mark_dir(mark: str, source: str) -> int:
            key = (mark, source)
            if key not in mark_dir_cache:
                mark_dir_cache[key] = _mark_net_direction(
                    state.market_trades, source, mark
                )
            return mark_dir_cache[key]

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

            # --- 1) original EMA logic ---
            if product in PASSIVE_QUOTE:
                orders = self._passive_quote(product, od, wall_mid,
                                             deviation, position, limit)
            elif product in TAKER_NEAR:
                orders = self._taker_near(product, od, wall_mid,
                                          deviation, position, limit)
            else:
                orders = self._taker_only(product, od, wall_mid,
                                          deviation, position, limit)

            # --- 2) follow layer ---
            mark = FOLLOW_MARK.get(product)
            source = FOLLOW_SOURCE.get(product)
            in_zone = self._ema_in_trade_zone(product, deviation)
            if not in_zone:
                # EMA 离开触发区，重置跟单计数（下次进入触发区从 0 重新数）
                if follow_counts.get(product):
                    follow_counts[product] = 0
            elif mark and source:
                net = mark_dir(mark, source)
                # 只在"方向一致的 Mark 净成交"时计数；反向或无成交都不计、不跟。
                if net != 0 and (
                    (net > 0 and deviation < 0) or (net < 0 and deviation > 0)
                ):
                    follow_counts[product] = follow_counts.get(product, 0) + 1
                    if follow_counts[product] >= FOLLOW_START_COUNT.get(product, 2):
                        # 达到起始计数，执行跟单
                        ema_signed = sum(o.quantity for o in orders)
                        proj_pos = position + ema_signed
                        max_qty = int(round(limit * FOLLOW_MAX_FRAC[product]))
                        if net > 0:
                            room = max(0, limit - proj_pos)
                            qty = min(max_qty, room, abs(net))
                            if qty > 0:
                                orders = orders + self._take_asks(
                                    product, qty, od, wall_mid,
                                    ACTIVE_CROSS_DISTANCE[product],
                                )
                        else:
                            room = max(0, limit + proj_pos)
                            qty = min(max_qty, room, abs(net))
                            if qty > 0:
                                orders = orders + self._take_bids(
                                    product, qty, od, wall_mid,
                                    ACTIVE_CROSS_DISTANCE[product],
                                )

            if orders:
                result[product] = orders

        return result, 0, json.dumps({"emas": emas, "follow_counts": follow_counts})

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _ema_in_trade_zone(product: str, deviation: float) -> bool:
        """True if |deviation| crosses the per-product entry/threshold —
        same gating that the EMA logic itself uses to allow new opens."""
        abs_dev = abs(deviation)
        if product in PASSIVE_QUOTE or product in TAKER_NEAR:
            return abs_dev >= ENTRY_THRESHOLD[product]
        return abs_dev >= TAKER_ONLY_THRESHOLD[product]

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