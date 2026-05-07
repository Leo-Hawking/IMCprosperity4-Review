"""Grid probing trader for EMERALDS and TOMATOES.

Main params:
1) Buy grid range [a, b].
2) Sell grid range [c, d].

For EMERALDS, ranges are absolute prices.
For TOMATOES, ranges are offsets around wall mid.

Emergency flatten is kept: if abs(position) reaches 75% of the product limit,
stop grid quoting and submit a fully marketable order to flatten.
"""

from __future__ import annotations

import math
from datamodel import Order, OrderDepth, TradingState


LIMITS: dict[str, int] = {
    "EMERALDS": 80,
    "TOMATOES": 80,
}

# EMERALDS absolute price ranges.
EMERALDS_BUY_RANGE_AB = (9991, 9994)
EMERALDS_SELL_RANGE_CD = (10006, 10008)

# TOMATOES ranges are offsets from wall mid.
TOMATOES_BUY_OFFSET_AB = (-8, -3)
TOMATOES_SELL_OFFSET_CD = (3, 8)

GRID_SIZE = 10
FLATTEN_TRIGGER_RATIO = 0.8

# "Market" prices for an aggressive crossing order.
MARKET_BUY_PRICE = 10**9
MARKET_SELL_PRICE = 0


class Trader:
    def run(self, state: TradingState):
        all_orders: dict[str, list[Order]] = {}

        for product, od in state.order_depths.items():
            if product not in LIMITS:
                all_orders[product] = []
                continue

            pos = state.position.get(product, 0)
            limit = LIMITS[product]
            trigger = math.ceil(limit * FLATTEN_TRIGGER_RATIO)

            if abs(pos) >= trigger and pos != 0:
                all_orders[product] = [self._flatten_now(product, pos)]
                continue

            if product == "EMERALDS":
                all_orders[product] = self._quote_emeralds(pos, limit)
            elif product == "TOMATOES":
                all_orders[product] = self._quote_tomatoes(od, pos, limit)
            else:
                all_orders[product] = []

        return all_orders, 0, ""

    def _quote_emeralds(self, pos: int, limit: int) -> list[Order]:
        buy_cap = max(0, limit - pos)
        sell_cap = max(0, limit + pos)
        orders: list[Order] = []

        buy_prices = self._build_buy_prices(*EMERALDS_BUY_RANGE_AB)
        sell_prices = self._build_sell_prices(*EMERALDS_SELL_RANGE_CD)

        for price in buy_prices:
            if buy_cap <= 0:
                break
            orders.append(Order("EMERALDS", price, GRID_SIZE))
            buy_cap -= GRID_SIZE

        for price in sell_prices:
            if sell_cap <= 0:
                break
            orders.append(Order("EMERALDS", price, -GRID_SIZE))
            sell_cap -= GRID_SIZE

        return orders

    def _quote_tomatoes(self, od: OrderDepth, pos: int, limit: int) -> list[Order]:
        if not od.buy_orders or not od.sell_orders:
            return []

        mid = round(self._wall_mid(od))
        buy_cap = max(0, limit - pos)
        sell_cap = max(0, limit + pos)
        orders: list[Order] = []

        buy_a, buy_b = TOMATOES_BUY_OFFSET_AB
        sell_c, sell_d = TOMATOES_SELL_OFFSET_CD

        buy_prices = self._build_buy_prices(mid + buy_a, mid + buy_b)
        sell_prices = self._build_sell_prices(mid + sell_c, mid + sell_d)

        for price in buy_prices:
            if buy_cap <= 0:
                break
            orders.append(Order("TOMATOES", price, GRID_SIZE))
            buy_cap -= GRID_SIZE

        for price in sell_prices:
            if sell_cap <= 0:
                break
            orders.append(Order("TOMATOES", price, -GRID_SIZE))
            sell_cap -= GRID_SIZE

        return orders

    @staticmethod
    def _flatten_now(product: str, pos: int) -> Order:
        if pos > 0:
            return Order(product, MARKET_SELL_PRICE, -pos)
        return Order(product, MARKET_BUY_PRICE, -pos)

    @staticmethod
    def _wall_mid(od: OrderDepth) -> float:
        bid_wall = max(od.buy_orders, key=lambda p: od.buy_orders[p])
        ask_wall = max(od.sell_orders, key=lambda p: abs(od.sell_orders[p]))
        return (bid_wall + ask_wall) / 2

    @staticmethod
    def _build_buy_prices(a: int, b: int) -> list[int]:
        low, high = sorted((a, b))
        # For buy side, quote from more aggressive (higher) to less aggressive.
        return list(range(high, low - 1, -1))

    @staticmethod
    def _build_sell_prices(c: int, d: int) -> list[int]:
        low, high = sorted((c, d))
        # For sell side, quote from more aggressive (lower) to less aggressive.
        return list(range(low, high + 1))