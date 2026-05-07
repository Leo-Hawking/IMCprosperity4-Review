

import json
from datamodel import Order, TradingState

LIMITS = {"EMERALDS": 80, "TOMATOES": 80}

# EMERALDS 参数
EMERALD_FAIR = 10000
EMERALD_NORMAL_OFFSET = 7     
EMERALD_CUT_OFFSET = 7         # 已无用
EMERALD_POS_HEAVY =   80      # 已无用

# TOMATOES 参数
TOMATO_PASSIVE_TRIGGER = 4
TOMATO_TAKER_POS_N = 60
TOMATO_TAKER_EDGE_A1 = 2 #小于pos用a1
TOMATO_TAKER_EDGE_A2 = 3 #大于用a2


class Trader:
    def run(self, state: TradingState):
        orders: dict[str, list[Order]] = {}
        for product in state.order_depths:
            if product == "EMERALDS":
                orders[product] = self._trade_emeralds(state)
            elif product == "TOMATOES":
                orders[product] = self._trade_tomatoes(state)
            else:
                orders[product] = []
        return orders, 0, json.dumps({})

    # ------------------------------------------------------------------
    # EMERALDS
    # ------------------------------------------------------------------
    def _trade_emeralds(self, state: TradingState) -> list[Order]:
        product = "EMERALDS"
        od = state.order_depths[product]
        pos = state.position.get(product, 0)
        limit = LIMITS[product]
        fair = EMERALD_FAIR

        orders: list[Order] = []
        buy_cap = limit - pos   # 还能买多少
        sell_cap = limit + pos  # 还能卖多少

        asks = sorted(od.sell_orders.items())              # 低价在前
        bids = sorted(od.buy_orders.items(), reverse=True)  # 高价在前

        # 1) 正 edge：吃所有严格越过 fair 的单
        for price, vol in asks:
            if price < fair and buy_cap > 0:
                qty = min(-vol, buy_cap)
                orders.append(Order(product, price, qty))
                buy_cap -= qty
                pos += qty

        for price, vol in bids:
            if price > fair and sell_cap > 0:
                qty = min(vol, sell_cap)
                orders.append(Order(product, price, -qty))
                sell_cap -= qty
                pos -= qty

        # 2) 0 edge：fair 档上有对手单时，沿着"减仓方向"吃掉
        if pos > 0:
            # 多头 → 可在 10000 卖出把仓位往回拉
            for price, vol in bids:
                if price == fair and sell_cap > 0 and pos > 0:
                    qty = min(vol, pos, sell_cap)
                    if qty > 0:
                        orders.append(Order(product, price, -qty))
                        sell_cap -= qty
                        pos -= qty
        elif pos < 0:
            for price, vol in asks:
                if price == fair and buy_cap > 0 and pos < 0:
                    qty = min(-vol, -pos, buy_cap)
                    if qty > 0:
                        orders.append(Order(product, price, qty))
                        buy_cap -= qty
                        pos += qty

        # 3) 限价挂单
        bid_px = fair - EMERALD_NORMAL_OFFSET
        ask_px = fair + EMERALD_NORMAL_OFFSET
        if pos > EMERALD_POS_HEAVY:            # 过度做多：把卖单前移到 ± 7 抢先减仓
            ask_px = fair + EMERALD_CUT_OFFSET
        elif pos < -EMERALD_POS_HEAVY:         # 过度做空：把买单前移到 ± 7
            bid_px = fair - EMERALD_CUT_OFFSET

        if buy_cap > 0:
            orders.append(Order(product, bid_px, buy_cap))
        if sell_cap > 0:
            orders.append(Order(product, ask_px, -sell_cap))

        return orders

    # ------------------------------------------------------------------
    # TOMATOES
    # ------------------------------------------------------------------
    def _trade_tomatoes(self, state: TradingState) -> list[Order]:
        product = "TOMATOES"
        od = state.order_depths[product]
        pos = state.position.get(product, 0)
        limit = LIMITS[product]

        orders: list[Order] = []
        if not od.buy_orders or not od.sell_orders:
            return orders

        fair = self._wall_mid(od)
        buy_cap = limit - pos
        sell_cap = limit + pos

        asks = sorted(od.sell_orders.items())
        bids = sorted(od.buy_orders.items(), reverse=True)

        # 1) 市价吃单：
        #    - 有利于头寸回归 0：必吃
        #    - 不利于头寸回归 0：满足 n/a1/a2 的 edge 判定才吃
        for price, vol in asks:
            edge = fair - price
            if price < fair and buy_cap > 0:
                if pos < 0:
                    # 买入会让空头回补，最多吃到刚好归零
                    qty = min(-vol, buy_cap, -pos)
                else:
                    taker_edge = (
                        TOMATO_TAKER_EDGE_A1
                        if abs(pos) <= TOMATO_TAKER_POS_N
                        else TOMATO_TAKER_EDGE_A2
                    )
                    if edge <= taker_edge:
                        continue
                    qty = min(-vol, buy_cap)
                if qty > 0:
                    orders.append(Order(product, price, qty))
                    buy_cap -= qty
                    pos += qty

        for price, vol in bids:
            edge = price - fair
            if price > fair and sell_cap > 0:
                if pos > 0:
                    # 卖出会让多头减仓，最多吃到刚好归零
                    qty = min(vol, sell_cap, pos)
                else:
                    taker_edge = (
                        TOMATO_TAKER_EDGE_A1
                        if abs(pos) <= TOMATO_TAKER_POS_N
                        else TOMATO_TAKER_EDGE_A2
                    )
                    if edge <= taker_edge:
                        continue
                    qty = min(vol, sell_cap)
                if qty > 0:
                    orders.append(Order(product, price, -qty))
                    sell_cap -= qty
                    pos -= qty

        # 2) 被动挂单：
        #    - 买侧：best_bid 距离 wall_mid > 4 时，best_bid+1 全仓挂买
        #    - 卖侧：best_ask 距离 wall_mid > 4 时，best_ask-1 全仓挂卖
        best_bid = bids[0][0]
        if fair - best_bid > TOMATO_PASSIVE_TRIGGER and buy_cap > 0:
            quote_px = best_bid + 1
            best_ask = asks[0][0]
            # 保证是被动单，避免 +1 后直接与最优卖盘成交
            if quote_px < best_ask:
                orders.append(Order(product, quote_px, buy_cap))

        best_ask = asks[0][0]
        if best_ask - fair > TOMATO_PASSIVE_TRIGGER and sell_cap > 0:
            quote_px = best_ask - 1
            # 保证是被动单，避免 -1 后直接与最优买盘成交
            if quote_px > best_bid:
                orders.append(Order(product, quote_px, -sell_cap))

        return orders

    # ------------------------------------------------------------------
    @staticmethod
    def _wall_mid(od) -> float:
        """fair = 最大买 wall 与最大卖 wall 的中点。"""
        bid_wall = max(od.buy_orders, key=lambda p: od.buy_orders[p])
        ask_wall = min(od.sell_orders, key=lambda p: abs(od.sell_orders[p]))
        return (bid_wall + ask_wall) / 2
