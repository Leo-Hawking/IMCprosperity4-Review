"""
Mean Reversion Strategy v3 for IMC Prosperity 3

Products and execution classes:

  PASSIVE_QUOTE  (HYDROGEL_PACK, VEV_4000):
      Four tiers based on |deviation|:
        stable  -> two-sided passive quotes (no take)
        entry   -> one-sided passive + eat near wall_mid
        active  -> cross-spread take, capped by ACTIVE_CROSS_DISTANCE
      Closing also uses tiered take (with active guard).

  TAKER_NEAR  (VELVETFRUIT_EXTRACT, VEV_4500):
      Two tiers:
        weak signal (|dev| > entry threshold) -> eat strictly within EAT_DISTANCE of wall_mid
        strong signal (|dev| > active threshold) -> active take, capped by ACTIVE_CROSS_DISTANCE

  TAKER_ONLY  (VEV_5000, 5100, 5200, 5300, 5400, 5500):
      Single tier:
        |dev| > threshold -> active take, capped by CROSS_LIMIT (strict)
      No passive quoting at all because these markets have very few takers.
"""

import json
import math
from typing import Dict, List

from datamodel import Order, OrderDepth, TradingState


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

# All products share the same EMA halflife. Long enough that the EMA does not chase noise.
HALFLIFE = 4000

# Fixed EMA priors. EMA seeds with these so a long halflife does not need warmup.
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

# Execution class membership.
PASSIVE_QUOTE = {"HYDROGEL_PACK", "VEV_4000"}
TAKER_NEAR = {"VELVETFRUIT_EXTRACT", "VEV_4500"}
TAKER_ONLY = {"VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"}

# --- thresholds ---

# PASSIVE_QUOTE and TAKER_NEAR both have a (weak, strong) pair.
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

# TAKER_ONLY has a single threshold.
TAKER_ONLY_THRESHOLD: Dict[str, float] = {
    "VEV_5000": 18.0,
    "VEV_5100": 17.0,
    "VEV_5200": 15.0,
    "VEV_5300": 8.0,
    "VEV_5400": 4.0,
    "VEV_5500": 4.0,
}

# --- execution constants ---

# How close to wall_mid we are willing to eat in a "weak signal eat near" step.
EAT_DISTANCE = 2          # strict: |price - wall_mid| < EAT_DISTANCE
IMPROVE_TICK = 1          # passive quote sits at best_bid+1 / best_ask-1

# Active-take guard distances. Strict: |price - wall_mid| < ACTIVE_CROSS_DISTANCE. fixed
ACTIVE_CROSS_DISTANCE: Dict[str, float] = {
    # PASSIVE_QUOTE
    "HYDROGEL_PACK": 8.0,
    "VEV_4000": 10.5,
    # TAKER_NEAR
    "VELVETFRUIT_EXTRACT": 3.0,
    "VEV_4500": 5.0,
    # TAKER_ONLY 
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


_ALPHA = _alpha(HALFLIFE)


def _wall_mid(od: OrderDepth):
    """wall_mid = midpoint of bid_with_max_volume and ask_with_max_volume.
    Tie-break: bid -> highest price, ask -> lowest price.
    Returns None if either side of the book is empty."""
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
        # restore EMA state
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

            # Update EMA. Seed with prior on first observation.
            prev = emas.get(product, INITIAL_EMA[product])
            ema = _ALPHA * wall_mid + (1.0 - _ALPHA) * prev
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
            else:  # TAKER_ONLY
                orders = self._taker_only(product, od, wall_mid,
                                          deviation, position, limit)

            if orders:
                result[product] = orders

        return result, 0, json.dumps({"emas": emas})

    # --- PASSIVE_QUOTE: 4 tiers ---------------------------------------------

    def _passive_quote(self, product, od, wall_mid, deviation, position, limit):
        entry = ENTRY_THRESHOLD[product]
        active = ACTIVE_THRESHOLD[product]
        abs_dev = abs(deviation)

        # Closing has priority. Close uses active-mode take (with cross guard).
        if position > 0 and deviation >= 0:
            return self._take_bids(product, position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])
        if position < 0 and deviation <= 0:
            return self._take_asks(product, -position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])

        # Stable region: two-sided passive quote.
        if abs_dev < entry:
            return self._stable_quote(product, position, limit, od)

        # Strong signal: active take.
        if abs_dev >= active:
            if deviation < 0:
                qty = limit - position
                return self._take_asks(product, qty, od, wall_mid,
                                       ACTIVE_CROSS_DISTANCE[product])
            else:
                qty = limit + position
                return self._take_bids(product, qty, od, wall_mid,
                                       ACTIVE_CROSS_DISTANCE[product])

        # Medium signal: eat near + passive quote on the rest.
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

    # --- TAKER_NEAR: 2 tiers, no passive ------------------------------------

    def _taker_near(self, product, od, wall_mid, deviation, position, limit):
        entry = ENTRY_THRESHOLD[product]
        active = ACTIVE_THRESHOLD[product]
        abs_dev = abs(deviation)

        # Closing: use the active guard distance.
        if position > 0 and deviation >= 0:
            return self._take_bids(product, position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])
        if position < 0 and deviation <= 0:
            return self._take_asks(product, -position, od, wall_mid,
                                   ACTIVE_CROSS_DISTANCE[product])

        if abs_dev < entry:
            return []

        # Choose distance: weak signal -> EAT_DISTANCE, strong -> active guard.
        guard = ACTIVE_CROSS_DISTANCE[product] if abs_dev >= active else EAT_DISTANCE

        if deviation < 0:
            qty = limit - position
            return self._take_asks(product, qty, od, wall_mid, guard)
        else:
            qty = limit + position
            return self._take_bids(product, qty, od, wall_mid, guard)

    # --- TAKER_ONLY: single tier --------------------------------------------

    def _taker_only(self, product, od, wall_mid, deviation, position, limit):
        thr = TAKER_ONLY_THRESHOLD[product]
        guard = ACTIVE_CROSS_DISTANCE[product]
        abs_dev = abs(deviation)

        # Closing.
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
    def _take_asks(product, qty, od: OrderDepth, wall_mid, max_diff):
        """Buy by walking asks ascending, stopping at strict price >= wall_mid + max_diff."""
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
    def _take_bids(product, qty, od: OrderDepth, wall_mid, max_diff):
        """Sell by walking bids descending, stopping at strict price <= wall_mid - max_diff."""
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
    def _passive_one_side(product, qty, od: OrderDepth, side: int):
        """Single-sided passive quote at best_bid+1 (side=+1) or best_ask-1 (side=-1)."""
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
    def _stable_quote(product, position, limit, od: OrderDepth):
        """Two-sided passive quote during stable regime."""
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