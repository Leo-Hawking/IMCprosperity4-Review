"""Round 1 策略（TEST FORK）：EMERALDS + TOMATOES

EMERALDS — 稳定资产，fair = 10000
    · 吃掉所有越过 10000 的单
    · 10000 上的 0-edge 单：方向有利于归零头寸时也吃
    · 正常挂 fair ± 8；|pos| > 12 时减仓侧缩到 fair ± 7

TOMATOES — 漂移资产，fair = wall_mid
    · 吃掉所有越过 wall_mid 的单（极端头寸方向除外）
    · 三级挂单：
        normal  (|pos| ≤ 80)     → fair ± 7
        mid     (|pos| ≤ 160)    → 减仓侧 fair ± 6/± 7 一起抢
        extreme (|pos| > 160)    → 减仓侧 fair ± 1/± 2，原侧停挂

前提：price impact 可忽略、限价单可以跟 wall 并排排队。
"""

import json
from datamodel import Order, TradingState

LIMITS = {"EMERALDS": 20, "TOMATOES": 80}

# EMERALDS 参数
EMERALD_FAIR = 10000
EMERALD_NORMAL_OFFSET = 7      # ± 8 — 成交密集档位
EMERALD_CUT_OFFSET = 6         # ± 7 — 头寸过大时的截胡档位
EMERALD_POS_HEAVY = 15         # |pos| > 12 触发截胡

# TOMATOES 参数
TOMATO_NORMAL_OFFSET = 6
TOMATO_MID_OFFSET = 3
TOMATO_EXTREME_OFFSETS = (1, 2)
TOMATO_POS_MID = 20            # |pos| > 80 进入 mid 档
TOMATO_POS_EXTREME = 40       # |pos| > 160 进入 extreme 档；该方向停吃


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
        fair_int = round(fair)

        buy_cap = limit - pos
        sell_cap = limit + pos

        asks = sorted(od.sell_orders.items())
        bids = sorted(od.buy_orders.items(), reverse=True)

        # 1) 市价吃单：越过 wall mid 的错报价 = 送钱
        #    极端头寸 + 不利方向 → 该方向停止吃单
        for price, vol in asks:
            if price < fair and buy_cap > 0 and pos <= TOMATO_POS_EXTREME:
                qty = min(-vol, buy_cap)
                orders.append(Order(product, price, qty))
                buy_cap -= qty
                pos += qty

        for price, vol in bids:
            if price > fair and sell_cap > 0 and pos >= -TOMATO_POS_EXTREME:
                qty = min(vol, sell_cap)
                orders.append(Order(product, price, -qty))
                sell_cap -= qty
                pos -= qty

        # 2) 三级限价挂单
        abs_pos = abs(pos)
        if abs_pos <= TOMATO_POS_MID:
            tier = "normal"
        elif abs_pos <= TOMATO_POS_EXTREME:
            tier = "mid"
        else:
            tier = "extreme"

        quotes: list[tuple[int, int]] = []  # (price, signed_qty)

        def place_bid(offset: int, size: int) -> None:
            if size > 0:
                quotes.append((fair_int - offset, size))

        def place_ask(offset: int, size: int) -> None:
            if size > 0:
                quotes.append((fair_int + offset, -size))

        if tier == "normal":
            # 对称双边 ± 7，penny the wall
            place_bid(TOMATO_NORMAL_OFFSET, buy_cap)
            place_ask(TOMATO_NORMAL_OFFSET, sell_cap)

        elif tier == "mid":
            # 减仓侧在 ± 6 和 ± 7 同时挂，截胡更多成交；原侧保持 ± 7
            if pos > 0:
                # 多头 → 减仓 = 卖
                half = sell_cap // 2
                place_ask(TOMATO_MID_OFFSET, half)
                place_ask(TOMATO_NORMAL_OFFSET, sell_cap - half)
                place_bid(TOMATO_NORMAL_OFFSET, buy_cap)
            else:
                half = buy_cap // 2
                place_bid(TOMATO_MID_OFFSET, half)
                place_bid(TOMATO_NORMAL_OFFSET, buy_cap - half)
                place_ask(TOMATO_NORMAL_OFFSET, sell_cap)

        else:  # extreme
            # 减仓侧激进到 ± 1~2，依靠 mid bot 稀疏成交平仓；原侧停挂
            off1, off2 = TOMATO_EXTREME_OFFSETS
            if pos > 0:
                half = sell_cap // 2
                place_ask(off1, half)
                place_ask(off2, sell_cap - half)
            else:
                half = buy_cap // 2
                place_bid(off1, half)
                place_bid(off2, buy_cap - half)

        for price, signed_qty in quotes:
            orders.append(Order(product, price, signed_qty))

        return orders

    # ------------------------------------------------------------------
    @staticmethod
    def _wall_mid(od) -> float:
        """fair = 最大买 wall 与最大卖 wall 的中点。"""
        bid_wall = max(od.buy_orders, key=lambda p: od.buy_orders[p])
        ask_wall = max(od.sell_orders, key=lambda p: abs(od.sell_orders[p]))
        return (bid_wall + ask_wall) / 2
