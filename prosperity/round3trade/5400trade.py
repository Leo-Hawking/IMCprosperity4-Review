from __future__ import annotations

import json

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


TARGET = "VEV_5400"
POSITION_LIMIT = 200


class Trader:
    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {symbol: [] for symbol in state.order_depths.keys()}

        od = state.order_depths.get(TARGET)
        if od is None:
            return result, 0, state.traderData or ""

        q = int(state.position.get(TARGET, 0))
        buy_cap = POSITION_LIMIT - q
        if buy_cap <= 0:
            return result, 0, state.traderData or ""

        orders: list[Order] = []

        for price in sorted(od.sell_orders.keys()):
            if buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price])
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, int(price), int(take)))
            buy_cap -= take

        if buy_cap > 0 and od.sell_orders:
            best_ask = min(od.sell_orders.keys())
            orders.append(Order(TARGET, int(best_ask), int(buy_cap)))

        result[TARGET] = orders
        return result, 0, state.traderData or ""
