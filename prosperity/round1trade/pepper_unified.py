"""INTARIAN_PEPPER_ROOT 统一框架做市策略.

实现 strategy_unified.md：
  Phase 1 建仓 → Phase 2 双边做市+主动吃单 → Phase 3 终局

模型：
  p_fair(t) = FAIR_BASE + MU · t            线性漂移
  P_SETTLE  = 12100                         终点结算价
  屏蔽订单簿 ⟹ 报价 = 对手最优价 ± ε

按 IMC Prosperity 提交格式：run(state) -> (orders_dict, conversions, trader_data)
"""

from __future__ import annotations

import json
import math

try:
    from datamodel import Order, TradingState
except ImportError:
    from prosperity4bt.datamodel import Order, TradingState


# ============================================================
# §1 参数表
# ============================================================
PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]
TARGET = "INTARIAN_PEPPER_ROOT"

# ── 已知常量 ──
P_SETTLE = 12_100
FAIR_BASE = 12_000
MU = 0.001
T_END = 100_000
N_MAX = 80
TICK = 1

# ── 需标定参数（先验值，后续可用日志回填）──
LAMBDA_B_VOL = 0.20   # bid 侧被动成交量率（量/单位时间，调高）
LAMBDA_A_VOL = 0.15   # ask 侧被动成交量率（调高）
DELTA_B = 1.0         # bid 半价差
DELTA_A = 1.0         # ask 半价差
GAMMA_PLUS = 0.05     # 非理性卖单到达率（我方买，调高）
GAMMA_MINUS = 0.03    # 非理性买单到达率（我方卖，调高）
XI_BAR = 3.0          # 越价幅度均值
BUILD_PREMIUM = 9     # Phase 1 建仓溢价容忍（tick），只在 Phase 1 生效

# ── 导出参数 ──
#   s_a_max = floor(2·δ_a·λ_b_vol / μ)
#   来源：一次做市周转中价差收益 ≥ 空仓漂移损失
#   在 μ 极小的参数域下公式会爆掉，因此加软上限 MAX_S_A_MAX
#   以保留 phase 结构（否则 K_BUF 吞并 N_MAX，Q_TARGET 塌缩到 0）
MAX_S_A_MAX = 8
_S_A_RAW = max(int(math.floor(2.0 * DELTA_A * LAMBDA_B_VOL / MU)), 1)
S_A_MAX = min(_S_A_RAW, MAX_S_A_MAX)
K_BUF = min(max(2 * S_A_MAX, 1), N_MAX // 2)
Q_TARGET = N_MAX - K_BUF
Q_FLOOR = max(N_MAX - 2 * K_BUF, 0)


# ============================================================
# §3 报价：屏蔽订单簿模型下，唯一可行报价 = 对手最优价 ± ε
# ============================================================
def _my_bid_price(best_bid_opp, last_bid_px, best_ask_opp, p_fair=None):
    """屏蔽订单簿 + ε，附加理性边界：bid 不得 ≥ p_fair（避免高买）."""
    if best_bid_opp is not None:
        px = best_bid_opp + TICK
    elif last_bid_px is not None:
        px = int(last_bid_px)
    else:
        return None
    if best_ask_opp is not None and px >= best_ask_opp:
        px = best_ask_opp - TICK
    if p_fair is not None and px >= p_fair:
        # 跨越 fair → 放弃该侧报价（做市是占优选择的前提是 bid < fair）
        return None
    return px if px > 0 else None


def _my_ask_price(best_ask_opp, last_ask_px, best_bid_opp, p_fair=None):
    """屏蔽订单簿 - ε，附加理性边界：ask 不得 ≤ p_fair（避免低卖）."""
    if best_ask_opp is not None:
        px = best_ask_opp - TICK
    elif last_ask_px is not None:
        px = int(last_ask_px)
    else:
        return None
    if best_bid_opp is not None and px <= best_bid_opp:
        px = best_bid_opp + TICK
    if p_fair is not None and px <= p_fair:
        return None
    return px if px > 0 else None


# ============================================================
# §4 阶段转换
# ============================================================
def _update_phase(phase, t, q, p_fair):
    remaining_drift = P_SETTLE - p_fair
    # μ / λ_b_vol ≈ 单次买回的期望漂移损失
    turnover_floor = MU / LAMBDA_B_VOL if LAMBDA_B_VOL > 0 else math.inf

    if phase == 1:
        # Phase 1 → 2：达到 q_target
        if q >= Q_TARGET:
            phase = 2
        # 边界：Phase 1 还没满就撞到终局
        if remaining_drift < turnover_floor:
            phase = 3
            return phase

    if phase == 2:
        if remaining_drift < turnover_floor:
            phase = 3

    return phase


# ============================================================
# §5 / §6 / §7 各阶段实现
# ============================================================
def _phase1(od, q, p_fair, last_bid_px, best_bid, best_ask, orders, memory):
    """建仓：激进吃 ask（阈值 = p_fair + BUILD_PREMIUM）+ 挂满 bid，不挂 ask.

    Phase 1 故意突破 fair 边界：P_SETTLE 确定保底，允许为建仓付溢价。
    """
    build_ceiling = p_fair + BUILD_PREMIUM
    buy_cap = N_MAX - q

    # ── 主动吃 ask：阈值 = p_fair + BUILD_PREMIUM（允许吃到 9 ticks 上方）──
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price > build_ceiling or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price])
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            q += take
            buy_cap -= take

    # ── 被动 bid 满额（软边界：bid ≤ build_ceiling，不突破建仓溢价）──
    if buy_cap > 0:
        px = _my_bid_price(best_bid, memory.get("last_bid_px"), best_ask,
                           p_fair=build_ceiling + 1)
        if px is not None:
            orders.append(Order(TARGET, int(px), buy_cap))
            memory["last_bid_px"] = int(px)

    return q


def _phase2(od, q, p_fair, best_bid, best_ask, orders, memory):
    """双边做市 + 主动吃单 + 仓位管理.

    关键约束：
      - taker 只吃越过 fair 的订单（ask < p_fair 或 bid > p_fair + 结算溢价）
      - 被动挂单不得跨越 p_fair（理性边界）
    """
    buy_cap = N_MAX - q

    # 记录被完全消耗的档位，用于后续重算 best_bid/best_ask
    consumed_asks: dict[int, int] = {}
    consumed_bids: dict[int, int] = {}

    # ── 6A 主动 take 买：严格 < p_fair 的非理性 ask ──
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price >= p_fair or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price])
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            consumed_asks[price] = consumed_asks.get(price, 0) + take
            q += take
            buy_cap -= take

    # ── 6A 主动 take 卖：扫描非理性 bid ──
    #   sell_threshold = μ/λ_b_vol + (P_SETTLE - p_fair) - δ_b
    #   含义：越价需同时覆盖漂移买回成本 + 放弃的结算利润
    #   且仓位必须 > q_floor
    if LAMBDA_B_VOL > 0:
        sell_threshold = MU / LAMBDA_B_VOL + (P_SETTLE - p_fair) - DELTA_B
    else:
        sell_threshold = math.inf
    sell_cap = max(q - Q_FLOOR, 0)
    if od.buy_orders and sell_cap > 0:
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if sell_cap <= 0:
                break
            xi = price - p_fair
            if xi <= sell_threshold:
                break
            avail = int(od.buy_orders[price])
            if avail <= 0:
                continue
            take = min(avail, sell_cap)
            orders.append(Order(TARGET, price, -take))
            consumed_bids[price] = consumed_bids.get(price, 0) + take
            q -= take
            sell_cap -= take

    # ── take 后重算 best_bid/best_ask（扣除被完全消耗的档位）──
    if consumed_asks:
        rem_asks = [p for p, v in od.sell_orders.items()
                    if -int(v) - consumed_asks.get(p, 0) > 0]
        best_ask = min(rem_asks) if rem_asks else None
    if consumed_bids:
        rem_bids = [p for p, v in od.buy_orders.items()
                    if int(v) - consumed_bids.get(p, 0) > 0]
        best_bid = max(rem_bids) if rem_bids else None

    # ── 6B 被动 bid：始终挂满（受理性边界约束）──
    buy_cap = N_MAX - q
    if buy_cap > 0:
        px = _my_bid_price(best_bid, memory.get("last_bid_px"), best_ask, p_fair)
        if px is not None:
            orders.append(Order(TARGET, int(px), buy_cap))
            memory["last_bid_px"] = int(px)

    # ── 6B 被动 ask：仅当 q > q_target 时，量 = min(q - q_target, s_a_max) ──
    if q > Q_TARGET and S_A_MAX >= 1:
        ask_size = min(q - Q_TARGET, S_A_MAX)
        px = _my_ask_price(best_ask, memory.get("last_ask_px"), best_bid, p_fair)
        if px is not None and ask_size > 0:
            orders.append(Order(TARGET, int(px), -ask_size))
            memory["last_ask_px"] = int(px)

    return q


def _phase3(od, q, p_fair, best_bid, best_ask, orders, memory):
    """终局：扫所有 < P_SETTLE 的 ask，bid 补满，不挂 ask."""
    buy_cap = N_MAX - q

    # ── 主动吃一切 ask < P_SETTLE ──
    if od.sell_orders and buy_cap > 0:
        for price in sorted(od.sell_orders.keys()):
            if price >= P_SETTLE or buy_cap <= 0:
                break
            avail = -int(od.sell_orders[price])
            if avail <= 0:
                continue
            take = min(avail, buy_cap)
            orders.append(Order(TARGET, price, take))
            q += take
            buy_cap -= take

    # ── 被动 bid 补满，价格不超过 P_SETTLE - ε ──
    if buy_cap > 0:
        px = _my_bid_price(best_bid, memory.get("last_bid_px"), best_ask)
        if px is not None:
            px = min(int(px), P_SETTLE - TICK)
            if px > 0:
                orders.append(Order(TARGET, int(px), buy_cap))
                memory["last_bid_px"] = int(px)

    return q


# ============================================================
# Trader 主类
# ============================================================
class Trader:
    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {p: [] for p in PRODUCTS}

        # 解析持久化状态
        memory: dict = {}
        if state.traderData:
            try:
                loaded = json.loads(state.traderData)
                if isinstance(loaded, dict):
                    memory = loaded
            except Exception:
                memory = {}

        t = int(state.timestamp)
        od = state.order_depths.get(TARGET)
        if od is None:
            return result, 0, json.dumps(memory)

        q = max(int(state.position.get(TARGET, 0)), 0)
        p_fair = int(FAIR_BASE + MU * t)

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        phase = int(memory.get("phase", 1))
        phase = _update_phase(phase, t, q, p_fair)
        memory["phase"] = phase

        orders: list[Order] = []
        if phase == 1:
            q = _phase1(od, q, p_fair, memory.get("last_bid_px"),
                        best_bid, best_ask, orders, memory)
        elif phase == 2:
            q = _phase2(od, q, p_fair, best_bid, best_ask, orders, memory)
        else:
            q = _phase3(od, q, p_fair, best_bid, best_ask, orders, memory)

        result[TARGET] = orders
        return result, 0, json.dumps(memory)
