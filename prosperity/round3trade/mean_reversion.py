"""
IMC Prosperity 3 - Round 3 "Gloves Off"
Per-product price mean-reversion (no IV / no surface / no delta hedge).

For every product:
    P_t  = (best_bid + best_ask) / 2
    F_t  = EMA(P_t, span)               # smoothed fair
    s_t2 = EMA((P_t - F_t)^2, span)     # rolling variance
    sigma_t = sqrt(s_t2)
    z_t  = (P_t - F_t) / sigma_t

Trade gate (must cover round-trip cost):
    buy : F_t - best_ask >  buffer  AND  z_t < -Z_OPEN
    sell: best_bid - F_t >  buffer  AND  z_t >  Z_OPEN

Sizing (linear in z, clipped to [0,1]):
    long  target = +LIMIT * clip(-z / Z_MAX, 0, 1)
    short target = -LIMIT * clip( z / Z_MAX, 0, 1)

Close: |z| < Z_CLOSE -> target = 0.

Per-product params (SPAN, Z_OPEN, Z_CLOSE, Z_MAX, BUFFER) are independent.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
from math import sqrt
import json


# ── position limits ─────────────────────────────────────────────────────────
LIMITS: Dict[str, int] = {
    "HYDROGEL_PACK":       200,
    "VELVETFRUIT_EXTRACT": 0,
    "VEV_4000":            0,
    "VEV_4500":            0,
    "VEV_5000":            0,
    "VEV_5100":            0,
    "VEV_5200":            0,
    "VEV_5300":            0,
    "VEV_5400":            0,
    "VEV_5500":            0,
    "VEV_6000":            0,
    "VEV_6500":            0,
}

# ── per-product hyper-parameters (filled by hyper-search) ───────────────────
# Keys:
#   span      : EMA span for F_t and var_t (in ticks)
#   z_open    : |z| threshold to open
#   z_close   : |z| threshold to flatten
#   z_max     : z at which target sizing saturates (>= z_open)
#   buffer    : minimum F-vs-quote edge required to fire (in seashells)
#   warmup    : ticks before any trade
DEFAULT_PARAMS = {
    "span":    1000,
    "z_open":  1.5,
    "z_close": 0,
    "z_max":   3.0,
    "buffer":  0.5,
    "warmup":  600,
}

PARAMS: Dict[str, Dict[str, float]] = {
    p: dict(DEFAULT_PARAMS) for p in LIMITS
}

# Prior fair value used to seed F before EMA span is filled.
PARAMS["HYDROGEL_PACK"]["mu"] = 10000.0
PARAMS["HYDROGEL_PACK"]["sigma0"] = 30.0   # σ prior, so z != ±1 on tick 1
PARAMS["HYDROGEL_PACK"]["warmup"] = 0      # mu seeds F; no need to wait

# ── safety ──────────────────────────────────────────────────────────────────
EPS = 1e-9


def best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def ema_update(prev: float, new: float, span: float) -> float:
    a = 2.0 / (span + 1.0)
    return a * new + (1.0 - a) * prev


def empty_state() -> Dict:
    return {"f": None, "v": 0.0, "n": 0}


def decide_target(z: float, F: float, best_bid: int, best_ask: int,
                  cur: int, limit: int, p: Dict[str, float]) -> int:
    z_open = p["z_open"]
    z_close = p["z_close"]
    z_max = max(p["z_max"], z_open + EPS)
    buf = p["buffer"]

    if abs(z) < z_close:
        return 0

    if z < -z_open and (F - best_ask) > buf:
        size = max(0.0, min(1.0, -z / z_max))
        return int(round(limit * size))

    if z > z_open and (best_bid - F) > buf:
        size = max(0.0, min(1.0, z / z_max))
        return -int(round(limit * size))

    return cur


def target_to_orders(symbol: str, target: int, cur: int,
                     best_bid: int, best_ask: int) -> List[Order]:
    delta = target - cur
    if delta == 0:
        return []
    if delta > 0:
        return [Order(symbol, best_ask, delta)]
    return [Order(symbol, best_bid, delta)]


class Trader:
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        conversions = 0

        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}
        states: Dict[str, Dict] = {p: mem.get(p, empty_state()) for p in LIMITS}

        for product, limit in LIMITS.items():
            ss = states[product]
            p = PARAMS.get(product, DEFAULT_PARAMS)
            cur = state.position.get(product, 0)
            od = state.order_depths.get(product)
            if od is None or not od.buy_orders or not od.sell_orders:
                continue

            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            mid = (best_bid + best_ask) / 2.0

            span = max(2.0, float(p["span"]))
            mu = p.get("mu", None)
            sigma0 = p.get("sigma0", None)
            if ss["f"] is None:
                ss["f"] = float(mu) if mu is not None else mid
                if sigma0 is not None:
                    ss["v"] = float(sigma0) ** 2
                else:
                    resid = mid - ss["f"]
                    ss["v"] = resid * resid if mu is not None else 0.0
            else:
                ss["f"] = ema_update(ss["f"], mid, span)
                resid = mid - ss["f"]
                ss["v"] = ema_update(ss["v"], resid * resid, span)
            ss["n"] += 1

            sigma = sqrt(ss["v"]) if ss["v"] > 0 else 0.0
            if sigma <= EPS or ss["n"] < int(p["warmup"]):
                target = cur
            else:
                z = (mid - ss["f"]) / sigma
                target = decide_target(z, ss["f"], best_bid, best_ask,
                                       cur, limit, p)

            target = max(-limit, min(limit, target))
            if target != cur:
                orders = target_to_orders(product, target, cur,
                                          best_bid, best_ask)
                if orders:
                    result[product] = orders

        return result, conversions, json.dumps(states)
