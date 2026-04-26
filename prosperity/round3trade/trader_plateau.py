"""
Mean Reversion Strategy for IMC Prosperity 3
Products: HYDROGEL_PACK, VELVETFRUIT_EXTRACT, VEV_4000

Common signal: deviation = wall_mid - EMA(wall_mid)
Differential execution:
  - HYDROGEL_PACK / VEV_4000 (wide spread): take asks/bids within EAT_DISTANCE of wall_mid;
    if none qualify, place passive at best_bid+1 / best_ask-1
  - VELVETFRUIT_EXTRACT (narrow spread): take directly, but not beyond wall_mid +/- WALL_MID_GUARD
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

PRODUCTS = ["HYDROGEL_PACK", "VELVETFRUIT_EXTRACT", "VEV_4000"]

POSITION_LIMITS: Dict[str, int] = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300,
}

# EMA halflife in ticks. VEV_4000 is intentionally locked to VELVETFRUIT_EXTRACT.
HALFLIFE: Dict[str, int] = {
    "HYDROGEL_PACK": 1200,
    "VELVETFRUIT_EXTRACT": 1500,
}
HALFLIFE["VEV_4000"] = HALFLIFE["VELVETFRUIT_EXTRACT"]

# Mean-reversion entry thresholds (in price units).
ENTRY_THRESHOLD: Dict[str, float] = {
    "HYDROGEL_PACK": 10,
    "VELVETFRUIT_EXTRACT": 20,
    "VEV_4000": 7.0,
}

# Execution constants
EAT_DISTANCE = 2        # passive-group: take if |price - wall_mid| < EAT_DISTANCE (strict)
IMPROVE_TICK = 1        # passive-group: undercut best by this many ticks
WALL_MID_GUARD = 3      # taker-group: do not cross wall_mid +/- WALL_MID_GUARD (strict)

# Execution groups
PASSIVE_PRODUCTS = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_PRODUCTS = {"VELVETFRUIT_EXTRACT"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alpha(halflife: int) -> float:
    """Convert halflife (ticks) to EMA smoothing factor."""
    return 1.0 - math.exp(-math.log(2.0) / halflife)


def _wall_mid(od: OrderDepth):
    """
    wall_mid = (bid_with_max_volume + ask_with_max_volume) / 2.
    Tie-break: bid side -> highest price, ask side -> lowest price.
    Returns None if either side of the book is empty.
    """
    if not od.buy_orders or not od.sell_orders:
        return None
    # buy_orders: positive volumes. We want max volume, then max price.
    bid_wall = max(od.buy_orders.keys(),
                   key=lambda p: (od.buy_orders[p], p))
    # sell_orders: negative volumes. Max abs volume == min raw value. Tie-break: min price.
    ask_wall = min(od.sell_orders.keys(),
                   key=lambda p: (od.sell_orders[p], p))
    return (bid_wall + ask_wall) / 2.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class Trader:
    def run(self, state: TradingState):
        # --- restore EMA state from traderData ---
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
                continue  # incomplete book this tick; skip EMA update + orders

            # --- update EMA ---
            a = _alpha(HALFLIFE[product])
            prev = emas.get(product)
            ema = wall_mid if prev is None else a * wall_mid + (1.0 - a) * prev
            emas[product] = ema

            # --- generate signal ---
            deviation = wall_mid - ema
            position = state.position.get(product, 0)
            limit = POSITION_LIMITS[product]
            thr = ENTRY_THRESHOLD[product]

            orders: List[Order] = []

            # 1) Closing has priority. Close when current position is on the wrong side of EMA.
            if position > 0 and deviation >= 0:
                orders = self._sell_orders(product, position, od, wall_mid)
            elif position < 0 and deviation <= 0:
                orders = self._buy_orders(product, -position, od, wall_mid)
            # 2) Otherwise consider opening.
            elif deviation <= -thr:
                qty = limit - position
                if qty > 0:
                    orders = self._buy_orders(product, qty, od, wall_mid)
            elif deviation >= thr:
                qty = limit + position
                if qty > 0:
                    orders = self._sell_orders(product, qty, od, wall_mid)

            if orders:
                result[product] = orders

        traderData = json.dumps({"emas": emas})
        conversions = 0
        return result, conversions, traderData

    # -----------------------------------------------------------------------
    # Order generation
    # -----------------------------------------------------------------------

    def _buy_orders(self, product: str, qty: int,
                    od: OrderDepth, wall_mid: float) -> List[Order]:
        if qty <= 0:
            return []

        if product in PASSIVE_PRODUCTS:
            # Step 1: scan for asks strictly within wall_mid + EAT_DISTANCE.
            cheap = [(p, -v) for p, v in od.sell_orders.items()
                     if p - wall_mid < EAT_DISTANCE]
            if cheap:
                cheap.sort()  # ascending price
                return self._consume_levels(product, qty, cheap, side=+1)

            # Step 2: no cheap asks -> place passive at best_bid + IMPROVE_TICK,
            #         provided it would not cross the book.
            if not od.buy_orders:
                return []
            px = max(od.buy_orders.keys()) + IMPROVE_TICK
            if od.sell_orders and px >= min(od.sell_orders.keys()):
                return []  # would cross; skip this tick
            return [Order(product, px, qty)]

        # Taker product: walk asks ascending until price exceeds the guard.
        levels: List = []
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > wall_mid + WALL_MID_GUARD:
                break
            levels.append((ask_price, -od.sell_orders[ask_price]))
        return self._consume_levels(product, qty, levels, side=+1)

    def _sell_orders(self, product: str, qty: int,
                     od: OrderDepth, wall_mid: float) -> List[Order]:
        if qty <= 0:
            return []

        if product in PASSIVE_PRODUCTS:
            cheap = [(p, v) for p, v in od.buy_orders.items()
                     if wall_mid - p < EAT_DISTANCE]
            if cheap:
                cheap.sort(reverse=True)  # descending price
                return self._consume_levels(product, qty, cheap, side=-1)

            if not od.sell_orders:
                return []
            px = min(od.sell_orders.keys()) - IMPROVE_TICK
            if od.buy_orders and px <= max(od.buy_orders.keys()):
                return []
            return [Order(product, px, -qty)]

        levels: List = []
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < wall_mid - WALL_MID_GUARD:
                break
            levels.append((bid_price, od.buy_orders[bid_price]))
        return self._consume_levels(product, qty, levels, side=-1)

    @staticmethod
    def _consume_levels(product: str, qty: int, levels, side: int) -> List[Order]:
        """Fill `qty` units across given (price, volume) levels in supplied order.
        side=+1 means buying (positive Order qty), side=-1 means selling."""
        orders: List[Order] = []
        remaining = qty
        for price, vol in levels:
            if remaining <= 0:
                break
            take = min(remaining, vol)
            if take <= 0:
                continue
            orders.append(Order(product, price, side * take))
            remaining -= take
        return orders
