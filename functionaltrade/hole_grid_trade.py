"""Grid quoting strategy for ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT.

For each product, place 1-lot limit orders on every level in fair +/- [1..7].
All quoted prices are rounded to integers.
"""

from __future__ import annotations

import json

try:
	from datamodel import Order, TradingState
except ImportError:
	from prosperity4bt.datamodel import Order, TradingState


PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]
GRID_OFFSETS = range(3, 8)  # +/-1 ... +/-7
ORDER_SIZE = 1

# Conservative default limits to avoid invalid aggregate orders near edges.
POSITION_LIMITS = {
	"ASH_COATED_OSMIUM": 80,
	"INTARIAN_PEPPER_ROOT": 80,
}


class Trader:
	def run(self, state: TradingState):
		memory = self._load_memory(state.traderData)

		result: dict[str, list[Order]] = {}
		for product in PRODUCTS:
			result[product] = []
			od = state.order_depths.get(product)
			if od is None:
				continue

			fair = self._fair_price(product, state, memory)
			fair_int = int(round(fair))
			result[product] = self._build_grid_orders(product, fair_int, state)

		trader_data = json.dumps(memory)
		conversions = 0
		return result, conversions, trader_data

	@staticmethod
	def _load_memory(trader_data: str) -> dict:
		if not trader_data:
			return {}
		try:
			loaded = json.loads(trader_data)
			return loaded if isinstance(loaded, dict) else {}
		except Exception:
			return {}

	def _fair_price(self, product: str, state: TradingState, memory: dict) -> float:
		if product == "INTARIAN_PEPPER_ROOT":
			# User-specified fair: int(12000 + 0.001 * t)
			return float(int(12000 + 0.001 * state.timestamp))

		# ASH_COATED_OSMIUM fair from largest-volume(>20) quotes with fallback.
		od = state.order_depths[product]
		bid_price = self._select_large_quote(od.buy_orders, is_bid=True)
		ask_price = self._select_large_quote(od.sell_orders, is_bid=False)

		last_bid = memory.get("ash_last_bid")
		last_ask = memory.get("ash_last_ask")

		use_bid = bid_price if bid_price is not None else last_bid
		use_ask = ask_price if ask_price is not None else last_ask

		if bid_price is not None:
			memory["ash_last_bid"] = bid_price
		if ask_price is not None:
			memory["ash_last_ask"] = ask_price

		if use_bid is not None and use_ask is not None:
			return (float(use_bid) + float(use_ask)) / 2.0

		# Final fallback if history is not yet available.
		if od.buy_orders and od.sell_orders:
			best_bid = max(od.buy_orders.keys())
			best_ask = min(od.sell_orders.keys())
			return (best_bid + best_ask) / 2.0

		return 0.0

	@staticmethod
	def _select_large_quote(side_orders: dict[int, int], is_bid: bool) -> int | None:
		"""Select price with max absolute volume among quotes where abs(volume) > 20."""
		if not side_orders:
			return None

		candidates: list[tuple[int, int]] = []
		for price, volume in side_orders.items():
			vol_abs = abs(int(volume))
			if vol_abs > 20:
				candidates.append((price, vol_abs))

		if not candidates:
			return None

		max_vol = max(v for _, v in candidates)
		prices = [p for p, v in candidates if v == max_vol]
		return max(prices) if is_bid else min(prices)

	def _build_grid_orders(self, product: str, fair_int: int, state: TradingState) -> list[Order]:
		orders: list[Order] = []

		pos = state.position.get(product, 0)
		limit = POSITION_LIMITS[product]
		buy_cap = max(0, limit - pos)
		sell_cap = max(0, limit + pos)

		# Buy grid: fair-1 ... fair-7
		for offset in GRID_OFFSETS:
			if buy_cap <= 0:
				break
			orders.append(Order(product, fair_int - offset, ORDER_SIZE))
			buy_cap -= ORDER_SIZE

		# Sell grid: fair+1 ... fair+7
		for offset in GRID_OFFSETS:
			if sell_cap <= 0:
				break
			orders.append(Order(product, fair_int + offset, -ORDER_SIZE))
			sell_cap -= ORDER_SIZE

		return orders
