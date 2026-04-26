from __future__ import annotations

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


TARGETS = ("VEV_6000", "VEV_6500")
POSITION_LIMIT = 200


class Trader:
    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {symbol: [] for symbol in state.order_depths.keys()}

        for sym in TARGETS:
            if sym not in state.order_depths:
                continue
            q = int(state.position.get(sym, 0))
            buy_cap = POSITION_LIMIT - q
            sell_cap = POSITION_LIMIT + q
            orders: list[Order] = []
            if buy_cap > 0:
                orders.append(Order(sym, 0, buy_cap))
            if sell_cap > 0:
                orders.append(Order(sym, 1, -sell_cap))
            result[sym] = orders

        return result, 0, state.traderData or ""
