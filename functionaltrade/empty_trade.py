"""Minimal empty strategy for IMC Prosperity submissions.

This trader intentionally places no orders while keeping the required
submission interface and return format.
"""

import json

try:
	# Competition runtime import path.
	from datamodel import Order, TradingState
except ImportError:
	# Local backtest fallback used in this workspace.
	from prosperity4bt.datamodel import Order, TradingState


PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]


class Trader:
	def bid(self):
		return 0

	def run(self, state: TradingState):
		# Return empty order lists for required products in required format.
		orders: dict[str, list[Order]] = {product: [] for product in PRODUCTS}
		conversions = 0
		trader_data = json.dumps({})
		return orders, conversions, trader_data
