"""INTARIAN_PEPPER_ROOT 实战策略 v3.

实现 strategy_v3.md：
  Phase 1 建仓：吃 ask ≤ fair + BUILD_PREMIUM，bid 补满，不挂 ask
  Phase 2 做市：
    - q == N：挂 ask(ASK_SIZE)，取消 bid，吃非理性 bid（bid > fair 且量 < IRR_TAKE_THRESHOLD）
    - q <  N：挂 bid(N-q)，取消 ask，吃非理性 ask（ask < fair）

模型：
    p_fair(t) = int(MU · t + b_t)
    b_t 由首 tick 锚定或滚动 CMA 维护（见 root_fair_calculate.md）
  屏蔽订单簿 ⟹ 报价 = 对手最优价 ± ε

按 IMC Prosperity 提交格式：run(state) -> (orders_dict, conversions, trader_data)
"""

from __future__ import annotations

import json

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


# ============================================================
# §0 参数
# ============================================================
PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]
TARGET = "INTARIAN_PEPPER_ROOT"

# ── 常量 ──
MU = 0.001
N = 80
TICK = 1
ANCHOR_STEP = 100
ANCHOR_TOL = 5
MIN_VALID_SIZE = 15
ONE_SIDE_PAD = 8

# ── 可调参数 ──
BUILD_PREMIUM = 7       # Phase 1 建仓溢价容忍
ASK_SPREAD_MIN = 0      # Phase 2 ask 最低距 fair 的距离
ASK_SIZE = 8            # Phase 2 ask 挂单量
IRR_TAKE_THRESHOLD = 8  # 非理性买单：对手量 < 此值才吃
BID_PREMIUM = 0         # Phase 2 bid ceiling = fair + BID_PREMIUM


# ============================================================
# §5 报价定位：屏蔽订单簿 ⟹ 最优对手价 ± ε，附真空回退
# ============================================================
def _my_bid_price(best_bid_opp, last_bid_px, best_ask_opp):
    if best_bid_opp is not None:
        px = best_bid_opp + TICK
    elif last_bid_px is not None:
        px = int(last_bid_px)
    else:
        return None
    if best_ask_opp is not None and px >= best_ask_opp:
        px = best_ask_opp - TICK
    return px if px > 0 else None


def _my_ask_price(best_ask_opp, last_ask_px, best_bid_opp):
    if best_ask_opp is not None:
        px = best_ask_opp - TICK
    elif last_ask_px is not None:
        px = int(last_ask_px)
    else:
        return None
    if best_bid_opp is not None and px <= best_bid_opp:
        px = best_bid_opp + TICK
    return px if px > 0 else None


def _round_to_step(x: float, step: int) -> int:
    return int(round(x / step) * step)


def _first_multiple_in_open_interval(lo: int, hi: int, step: int) -> int | None:
    if lo >= hi:
        return None
    first = ((lo // step) + 1) * step
    return first if first < hi else None


def _best_valid_bid(od) -> int | None:
    valid = [p for p, sz in od.buy_orders.items() if int(sz) >= MIN_VALID_SIZE]
    return max(valid) if valid else None


def _best_valid_ask(od) -> int | None:
    valid = [p for p, sz in od.sell_orders.items() if -int(sz) >= MIN_VALID_SIZE]
    return min(valid) if valid else None


def _calc_fair_price(od, t: int, memory: dict) -> int:
    """根据规格计算 fair：首 tick 锚定，失败后使用滚动 CMA 维护 b_t。"""
    fair_state = memory.setdefault("fair_state", {})
    initialized = bool(fair_state.get("initialized", False))

    if not initialized:
        fair_state["initialized"] = True
        fair_state["rolling"] = False
        fair_state["t0"] = t

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        b0: float | None = None
        if best_bid is not None and best_ask is not None:
            mid = 0.5 * (best_bid + best_ask)
            b_target = _round_to_step(mid, ANCHOR_STEP)
            if abs(mid - b_target) <= ANCHOR_TOL:
                b0 = float(b_target)
            else:
                fair_state["rolling"] = True
        elif best_bid is not None:
            b_target = _first_multiple_in_open_interval(
                int(best_bid), int(best_bid + 10), ANCHOR_STEP
            )
            if b_target is not None:
                b0 = float(b_target)
            else:
                b0 = float(best_bid + ONE_SIDE_PAD)
                fair_state["rolling"] = True
        elif best_ask is not None:
            b_target = _first_multiple_in_open_interval(
                int(best_ask - 10), int(best_ask), ANCHOR_STEP
            )
            if b_target is not None:
                b0 = float(b_target)
            else:
                b0 = float(best_ask - ONE_SIDE_PAD)
                fair_state["rolling"] = True

        if b0 is None:
            b0 = 12000.0
            fair_state["rolling"] = True

        fair_state["b"] = b0
        fair_state["cma_n"] = 0

    b_prev = float(fair_state.get("b", 12000.0))

    valid_bid = _best_valid_bid(od)
    valid_ask = _best_valid_ask(od)

    if valid_bid is not None:
        fair_state["last_valid_bid"] = int(valid_bid)
    else:
        last_bid = fair_state.get("last_valid_bid")
        if last_bid is not None:
            valid_bid = int(last_bid)

    if valid_ask is not None:
        fair_state["last_valid_ask"] = int(valid_ask)
    else:
        last_ask = fair_state.get("last_valid_ask")
        if last_ask is not None:
            valid_ask = int(last_ask)

    mid_t: float | None = None
    if valid_bid is not None and valid_ask is not None:
        mid_t = 0.5 * (valid_bid + valid_ask)
    elif valid_bid is not None:
        mid_t = float(valid_bid + ONE_SIDE_PAD)
    elif valid_ask is not None:
        mid_t = float(valid_ask - ONE_SIDE_PAD)

    if fair_state.get("rolling", False) and mid_t is not None:
        n = int(fair_state.get("cma_n", 0)) + 1
        b_t = b_prev + ((mid_t - MU * t) - b_prev) / n
        fair_state["b"] = b_t
        fair_state["cma_n"] = n
        b_used = b_t
    else:
        b_used = b_prev

    memory["fair_state"] = fair_state
    return int(MU * t + b_used)


# ============================================================
# §3 Phase 1：建仓
# ============================================================
def _phase1(od, q, p_fair, best_bid, best_ask, orders, memory, consumed_asks):
    """吃 ask ≤ fair + BUILD_PREMIUM 直到满仓，bid 补仓，不挂 ask."""
    take_ceiling = p_fair + BUILD_PREMIUM
    buy_cap = N - q

    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price > take_ceiling or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    if buy_cap > 0:
        px = _my_bid_price(best_bid, memory.get("last_bid_px"), best_ask)
        if px is not None:
            orders.append(Order(TARGET, int(px), buy_cap))
            memory["last_bid_px"] = int(px)

    return q


# ============================================================
# §4 Phase 2：满仓做市
# ============================================================
def _phase2_take_irrational(od, q, p_fair, orders,
                            consumed_asks, consumed_bids):
    """4A 主动吃非理性单."""
    # ── 买入侧：吃所有 ask < fair ──
    buy_cap = N - q
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price >= p_fair or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price]) - consumed_asks.get(price, 0)
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    # ── 卖出侧：满仓时，吃 bid > fair 且对手量 < IRR_TAKE_THRESHOLD ──
    if q >= N and od.buy_orders:
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= p_fair or q <= 0:
                break
            avail = int(od.buy_orders[price]) - consumed_bids.get(price, 0)
            if avail <= 0 or avail >= IRR_TAKE_THRESHOLD:
                continue
            take = min(avail, q)
            orders.append(Order(TARGET, price, -take))
            consumed_bids[price] = consumed_bids.get(price, 0) + take
            q -= take

    return q


def _phase2_manage_ask(od, q, p_fair, best_bid, best_ask, orders, memory,
                      consumed_asks):
    """4B 满仓挂 ask：不低于 fair + ASK_SPREAD_MIN."""
    # 重算 best_ask（扣除被消耗档位）
    if consumed_asks:
        rem = [p for p, v in od.sell_orders.items()
               if -int(v) - consumed_asks.get(p, 0) > 0]
        best_ask_eff = min(rem) if rem else None
    else:
        best_ask_eff = best_ask

    px = _my_ask_price(best_ask_eff, memory.get("last_ask_px"), best_bid)
    if px is None:
        return
    ask_floor = p_fair + ASK_SPREAD_MIN
    if px < ask_floor:
        px = ask_floor
    if ASK_SIZE > 0:
        orders.append(Order(TARGET, int(px), -ASK_SIZE))
        memory["last_ask_px"] = int(px)


def _phase2_manage_bid(od, q, p_fair, best_bid, best_ask, orders, memory,
                      consumed_bids):
    """4C 非满仓挂 bid：不高于 fair + BID_PREMIUM，量 = N - q."""
    if consumed_bids:
        rem = [p for p, v in od.buy_orders.items()
               if int(v) - consumed_bids.get(p, 0) > 0]
        best_bid_eff = max(rem) if rem else None
    else:
        best_bid_eff = best_bid

    px = _my_bid_price(best_bid_eff, memory.get("last_bid_px"), best_ask)
    if px is None:
        return
    bid_ceiling = p_fair + BID_PREMIUM
    if px > bid_ceiling:
        px = bid_ceiling
    size = N - q
    if size > 0 and px > 0:
        orders.append(Order(TARGET, int(px), size))
        memory["last_bid_px"] = int(px)


# ============================================================
# Trader 主类（§1 主循环）
# ============================================================
class Trader:
    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {p: [] for p in PRODUCTS}

        memory: dict = {}
        if state.traderData:
            try:
                loaded = json.loads(state.traderData)
                if isinstance(loaded, dict):
                    memory = loaded
            except Exception:
                memory = {}

        od = state.order_depths.get(TARGET)
        if od is None:
            return result, 0, json.dumps(memory)

        t = int(state.timestamp)
        q = max(int(state.position.get(TARGET, 0)), 0)
        p_fair = _calc_fair_price(od, t, memory)

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        orders: list[Order] = []
        consumed_asks: dict[int, int] = {}
        consumed_bids: dict[int, int] = {}

        # ── 阶段转换：一旦达到 N 即进入 Phase 2，不回退 ──
        phase = int(memory.get("phase", 1))
        if phase == 1 and q >= N:
            phase = 2
        memory["phase"] = phase

        # ── 执行当前阶段 ──
        if phase == 1:
            q = _phase1(od, q, p_fair, best_bid, best_ask,
                        orders, memory, consumed_asks)
        else:
            q = _phase2_take_irrational(od, q, p_fair, orders,
                                        consumed_asks, consumed_bids)
            if q >= N:
                _phase2_manage_ask(od, q, p_fair, best_bid, best_ask,
                                   orders, memory, consumed_asks)
            else:
                _phase2_manage_bid(od, q, p_fair, best_bid, best_ask,
                                   orders, memory, consumed_bids)
            if q >= N and phase == 1:
                phase = 2
                memory["phase"] = 2

        result[TARGET] = orders
        return result, 0, json.dumps(memory)