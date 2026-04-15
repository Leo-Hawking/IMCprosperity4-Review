"""确定性上涨市场 INTARIAN_PEPPER_ROOT 做市策略 v0.

实现 strategy_pseudocode_v0.md 中描述的价值函数市商：
- 初始化时一次性反向归纳求解 V[i][q]
- 主循环：被动双边挂单 + 扫书吃非理性单 + 终盘强平
- 产品为 long-only：q ∈ {0, 1, ..., N}

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
# 全局常数 (pseudocode §0)
# ============================================================
PRODUCTS = ["ASH_COATED_OSMIUM", "INTARIAN_PEPPER_ROOT"]
TARGET = "INTARIAN_PEPPER_ROOT"

MU = 0.001              # 价格漂移（每 timestamp 单位）
T_END = 100_000         # 交易结束时刻
N_MAX = 80              # 单向最大持仓
TICK = 1                # 最小 tick ε
SPREAD = 2.0            # σ：可捕获的价差
S_MAX = 5               # 单次考虑的最大成交量
XI_HAT = 2.0            # 非理性越价幅度均值
FAIR_BASE = 12_000      # fair = int(MU·t + FAIR_BASE)

# DP 网格：M_STEPS 个时间格，每格覆盖 T_END / M_STEPS 个 timestamp
M_STEPS = 100
CELL_SPAN = T_END / M_STEPS  # 每格覆盖的 timestamp 数

# 每格到达率（pseudocode §6 示例表，按需调参）
# 下标 s ∈ 1..S_MAX；0 位占位
LAMBDA_B = [0.0, 0.50, 0.20, 0.08, 0.03, 0.01]
LAMBDA_A = [0.0, 0.40, 0.15, 0.06, 0.02, 0.01]
GAMMA_P = [0.0, 0.05, 0.02, 0.005, 0.001, 0.0005]   # 非理性卖单(我方买)
GAMMA_N = [0.0, 0.03, 0.01, 0.003, 0.001, 0.0003]   # 非理性买单(我方卖)

# ============================================================
# !!! 硬编码：最后 STOP_TRADING_WINDOW 个 timestamp 完全停止交易 !!!
# 原因：临近终盘时 V 的边界效应使得挂单的预期收益评估不可靠，
# 直接返回空订单集以避免在收尾阶段累积意外头寸。
# ============================================================
STOP_TRADING_WINDOW = 2_000


# ============================================================
# 价值函数反向归纳 (pseudocode §1)
# ============================================================
def _solve_value_function():
    """返回 (V, dV)，其中 V[i][q]=步 i 持仓 q 的预期剩余 PnL，
    dV[i][q]=V[i][q]-V[i][q-1]，dV[i][0]=+inf 作为哨兵。"""
    M = M_STEPS
    N = N_MAX
    V = [[0.0] * (N + 1) for _ in range(M + 1)]

    # 一格内的单位漂移收益（对应 pseudocode 中 q·μ·dt）
    drift_per_cell = MU * CELL_SPAN

    for i in range(M - 1, -1, -1):
        Vi1 = V[i + 1]
        Vi = V[i]
        for q in range(N + 1):
            drift = q * drift_per_cell

            # --- ① 被动 bid 成交 ---
            bid_term = 0.0
            if q < N:
                room = N - q
                s_cap = min(S_MAX, room)
                for s in range(1, s_cap + 1):
                    val = Vi1[q + s] - Vi1[q] + s * SPREAD / 2.0
                    if val > 0:
                        bid_term += LAMBDA_B[s] * val
                for s in range(room + 1, S_MAX + 1):
                    val = Vi1[N] - Vi1[q] + room * SPREAD / 2.0
                    if val > 0:
                        bid_term += LAMBDA_B[s] * val

            # --- ② 被动 ask 成交 ---
            ask_term = 0.0
            if q > 0:
                s_cap = min(S_MAX, q)
                for s in range(1, s_cap + 1):
                    val = Vi1[q - s] - Vi1[q] + s * SPREAD / 2.0
                    if val > 0:
                        ask_term += LAMBDA_A[s] * val
                for s in range(q + 1, S_MAX + 1):
                    val = Vi1[0] - Vi1[q] + q * SPREAD / 2.0
                    if val > 0:
                        ask_term += LAMBDA_A[s] * val

            # --- ③ 非理性卖单，我方主动 take buy ---
            take_buy = 0.0
            if q < N:
                for v in range(1, S_MAX + 1):
                    cap = min(v, N - q)
                    total = 0.0
                    for j in range(1, cap + 1):
                        marginal = XI_HAT + Vi1[q + j] - Vi1[q + j - 1]
                        if marginal > 0:
                            total += marginal
                        else:
                            break
                    take_buy += GAMMA_P[v] * total

            # --- ④ 非理性买单，我方主动 take sell ---
            take_sell = 0.0
            if q > 0:
                for v in range(1, S_MAX + 1):
                    cap = min(v, q)
                    surplus = 0.0
                    for j in range(1, cap + 1):
                        marginal_hold = Vi1[q - j + 1] - Vi1[q - j]
                        if XI_HAT > marginal_hold:
                            surplus += XI_HAT - marginal_hold
                        else:
                            break
                    take_sell += GAMMA_N[v] * surplus

            Vi[q] = drift + bid_term + ask_term + take_buy + take_sell + Vi1[q]

    dV = [[0.0] * (N + 1) for _ in range(M + 1)]
    for i in range(M + 1):
        dV[i][0] = math.inf
        for q in range(1, N + 1):
            dV[i][q] = V[i][q] - V[i][q - 1]
    return V, dV


# ============================================================
# 持久化辅助
# ============================================================
def _load_memory(trader_data: str) -> dict:
    if not trader_data:
        return {}
    try:
        loaded = json.loads(trader_data)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


# ============================================================
# Trader 主类
# ============================================================
class Trader:
    def __init__(self) -> None:
        self._V, self._dV = _solve_value_function()

    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {p: [] for p in PRODUCTS}

        # 解析上一时刻持久化的挂价（用于单边空档时的回退报价）
        memory = _load_memory(state.traderData)
        last_bid_px = memory.get("last_bid_px")
        last_ask_px = memory.get("last_ask_px")

        t = int(state.timestamp)

        # ================================================================
        # !!! 硬编码：最后 STOP_TRADING_WINDOW 个 timestamp 完全停止交易 !!!
        # 不发任何订单（既不挂也不吃），直接返回空 orders。
        # ================================================================
        if T_END - t <= STOP_TRADING_WINDOW:
            return result, 0, json.dumps(memory)

        od = state.order_depths.get(TARGET)
        if od is None:
            return result, 0, json.dumps(memory)

        q = int(state.position.get(TARGET, 0))
        # long-only 策略：把任何意外的负仓位视作 0 参考
        q = max(q, 0)

        fair = int(FAIR_BASE + MU * t)
        i = min(int(t / CELL_SPAN), M_STEPS - 1)
        Vi = self._V[i]
        dVi = self._dV[i]

        orders: list[Order] = []

        # ---------- ④ 主动吃非理性卖单（低于 fair 的 ask） ----------
        buy_cap = max(0, N_MAX - q)
        sell_cap = q  # 当前可卖数量 = 多头仓位

        if od.sell_orders:
            for price in sorted(od.sell_orders.keys()):
                if buy_cap <= 0 or price >= fair:
                    break
                avail = -int(od.sell_orders[price])  # 卖盘量以负数存储
                if avail <= 0:
                    continue
                xi = fair - price
                take = 0
                step_cap = min(avail, buy_cap)
                for j in range(1, step_cap + 1):
                    q_next = q + j
                    if q_next > N_MAX:
                        break
                    marginal_hold = Vi[q_next] - Vi[q_next - 1]
                    if xi + marginal_hold > 0:
                        take = j
                    else:
                        break
                if take > 0:
                    orders.append(Order(TARGET, price, take))
                    q += take
                    buy_cap -= take
                    sell_cap += take

        # ---------- ⑤ 主动吃非理性买单（高于 fair 的 bid） ----------
        if od.buy_orders:
            for price in sorted(od.buy_orders.keys(), reverse=True):
                if sell_cap <= 0 or price <= fair:
                    break
                avail = int(od.buy_orders[price])
                if avail <= 0:
                    continue
                xi = price - fair
                take = 0
                step_cap = min(avail, sell_cap)
                for j in range(1, step_cap + 1):
                    q_idx = q - j + 1
                    if q_idx < 1:
                        break
                    if xi > dVi[q_idx]:
                        take = j
                    else:
                        break
                if take > 0:
                    orders.append(Order(TARGET, price, -take))
                    q -= take
                    buy_cap += take
                    sell_cap -= take

        best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
        best_ask = min(od.sell_orders.keys()) if od.sell_orders else None

        # ---------- ② 被动 bid ----------
        if buy_cap > 0:
            room = buy_cap  # = N - q
            s_cap = min(S_MAX, room)
            e_bid = 0.0
            for s in range(1, s_cap + 1):
                e_bid += LAMBDA_B[s] * (Vi[q + s] - Vi[q] + s * SPREAD / 2.0)
            for s in range(room + 1, S_MAX + 1):
                e_bid += LAMBDA_B[s] * (Vi[N_MAX] - Vi[q] + room * SPREAD / 2.0)

            if e_bid > 0:
                # 首选：比市场最高买单优一档
                if best_bid is not None:
                    px = best_bid + TICK
                # 单边空档：沿用上一时刻挂价
                elif last_bid_px is not None:
                    px = int(last_bid_px)
                else:
                    px = fair - TICK
                if best_ask is not None and px >= best_ask:
                    px = best_ask - TICK
                if px > 0:
                    orders.append(Order(TARGET, int(px), buy_cap))
                    memory["last_bid_px"] = int(px)

        # ---------- ③ 被动 ask ----------
        if sell_cap > 0:
            room = sell_cap  # = q
            s_cap = min(S_MAX, room)
            e_ask = 0.0
            for s in range(1, s_cap + 1):
                e_ask += LAMBDA_A[s] * (Vi[q - s] - Vi[q] + s * SPREAD / 2.0)
            for s in range(room + 1, S_MAX + 1):
                e_ask += LAMBDA_A[s] * (Vi[0] - Vi[q] + room * SPREAD / 2.0)

            if e_ask > 0:
                # 首选：比市场最低卖单优一档
                if best_ask is not None:
                    px = best_ask - TICK
                # 单边空档：沿用上一时刻挂价
                elif last_ask_px is not None:
                    px = int(last_ask_px)
                else:
                    px = fair + TICK
                if best_bid is not None and px <= best_bid:
                    px = best_bid + TICK
                orders.append(Order(TARGET, int(px), -sell_cap))
                memory["last_ask_px"] = int(px)

        result[TARGET] = orders
        return result, 0, json.dumps(memory)
