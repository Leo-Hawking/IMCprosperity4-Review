"""
IMC Prosperity 3 - Round 3 "Gloves Off"
VEV voucher mean-reversion strategy (5 strikes near ATM).

Per-strike pipeline:
    iv_hat   = 0.0119 * m_t + 0.2441,  m_t = ln(K/S)/sqrt(TTE_days)
    bs_theo  = BS_call(S, K, TTE_years, iv_hat)        (r = q = 0)
    resid    = mid - bs_theo
    mu       = EMA(resid,        SPAN_LONG)
    abs_dev  = EMA(|resid - mu|, SPAN_LONG)
    rec_dev  = EMA(|resid - mu|, SPAN_SHORT)
    score    = (resid - mu) / abs_dev

    target = -LIMIT  if tradeable and score >  THR_OPEN
    target = +LIMIT  if tradeable and score < -THR_OPEN
    target =  0      if |score| < THR_CLOSE
    target =  cur    otherwise
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
from math import log, sqrt, erf
import json
import os


# ---- universe ----
UNDERLYING = "VELVETFRUIT_EXTRACT"
STRIKES = [5000, 5100, 5200, 5300, 5500]
LIMIT = 300

# ---- time constants ----
TS_PER_DAY = 1_000_000
DAYS_PER_YEAR = 365

# Live: Round 3 starts at TTE = 5.0d.
# Backtest: each historical "day_N" of Round 3 data corresponds to a different
# starting TTE per spec §2.1. Override by env var when bt-driving the trader.
#   day 0 -> 8.0d (tutorial)
#   day 1 -> 7.0d (Round 1)
#   day 2 -> 6.0d (Round 2)
_DAY_TO_TTE = {0: 8.0, 1: 7.0, 2: 6.0}
_BT_DAY_ENV = os.environ.get("VEV_BT_DAY")
if _BT_DAY_ENV is not None:
    ROUND_START_TTE = _DAY_TO_TTE.get(int(_BT_DAY_ENV), 5.0)
else:
    ROUND_START_TTE = float(os.environ.get("VEV_ROUND_START_TTE", "5.0"))

# ---- iv_hat (offline-calibrated, frozen) ----
IV_HAT_SLOPE = 0.0119
IV_HAT_INTERCEPT = 0.2441

# ---- EMA windows / thresholds (env-overridable for hyper search) ----
SPAN_LONG = int(os.environ.get("VEV_SPAN_LONG", "50000"))
SPAN_SHORT = int(os.environ.get("VEV_SPAN_SHORT", "10000"))
WARMUP_TICKS = int(os.environ.get("VEV_WARMUP_TICKS", str(3 * SPAN_LONG)))

THR_OPEN = float(os.environ.get("VEV_THR_OPEN", "1.5"))
THR_CLOSE = float(os.environ.get("VEV_THR_CLOSE", "0.3"))
MIN_DEV = float(os.environ.get("VEV_MIN_DEV", "0.5"))

# ---- safety ----
EPS = 1e-6
MAX_SPREAD = 5.0       # if spread > this, skip ordering (still update EMA)
JUMP_K = 5.0           # mid jump > JUMP_K * abs_dev -> bad tick, skip


# ---------- Black-Scholes ----------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_call_price(S: float, K: float, T_years: float, sigma: float) -> float:
    if T_years <= 0 or sigma <= 0 or S <= 0:
        return max(S - K, 0.0)
    sqrt_T = sqrt(T_years)
    d1 = (log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def compute_iv_hat(S: float, K: float, T_days: float) -> float:
    m_t = log(K / S) / sqrt(T_days)
    return IV_HAT_SLOPE * m_t + IV_HAT_INTERCEPT


# ---------- TTE ----------
def compute_TTE_days(global_ts: int) -> float:
    progress = (global_ts % TS_PER_DAY) / TS_PER_DAY
    return ROUND_START_TTE - progress


# ---------- EMA ----------
def ema_update(prev: float, new_val: float, span: int) -> float:
    alpha = 2.0 / (span + 1.0)
    return alpha * new_val + (1.0 - alpha) * prev


# ---------- order book helpers ----------
def best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def mid_price(od: OrderDepth) -> Optional[float]:
    bid, ask = best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


# ---------- decision ----------
def decide_target(score: float, recent_dev: float, current_pos: int) -> int:
    tradeable = recent_dev > MIN_DEV
    if tradeable and score > THR_OPEN:
        return -LIMIT
    if tradeable and score < -THR_OPEN:
        return LIMIT
    if abs(score) < THR_CLOSE:
        return 0
    return current_pos


def target_to_orders(symbol: str, target_pos: int, current_pos: int,
                     best_bid: int, best_ask: int) -> List[Order]:
    delta = target_pos - current_pos
    if delta == 0:
        return []
    if delta > 0:
        return [Order(symbol, best_ask, delta)]
    return [Order(symbol, best_bid, delta)]


def liquidate_orders(symbol: str, current_pos: int,
                     best_bid: Optional[int], best_ask: Optional[int]) -> List[Order]:
    if current_pos == 0:
        return []
    if current_pos > 0 and best_bid is not None:
        return [Order(symbol, best_bid, -current_pos)]
    if current_pos < 0 and best_ask is not None:
        return [Order(symbol, best_ask, -current_pos)]
    return []


# ---------- per-strike state ----------
def empty_state() -> Dict:
    return {
        "mu": 0.0,
        "abs_dev": 0.0,
        "recent_dev": 0.0,
        "n": 0,
        "last_mid": None,
    }


class Trader:
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        conversions = 0

        # ---- restore persistent state ----
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}
        strike_states = {
            K: mem.get(str(K), empty_state()) for K in STRIKES
        }

        # ---- common quantities ----
        od_u = state.order_depths.get(UNDERLYING)
        S = mid_price(od_u) if od_u is not None else None
        if S is None or S <= 0:
            return result, conversions, json.dumps(self._dump(strike_states))

        TTE_days = compute_TTE_days(state.timestamp)
        TTE_years = TTE_days / DAYS_PER_YEAR

        # near-expiry: liquidate everything, no new opens
        if TTE_days < 0.01:
            for K in STRIKES:
                sym = f"VEV_{K}"
                cur = state.position.get(sym, 0)
                od = state.order_depths.get(sym)
                if cur == 0 or od is None:
                    continue
                bb, ba = best_bid_ask(od)
                orders = liquidate_orders(sym, cur, bb, ba)
                if orders:
                    result[sym] = orders
            return result, conversions, json.dumps(self._dump(strike_states))

        # ---- per-strike loop ----
        for K in STRIKES:
            sym = f"VEV_{K}"
            ss = strike_states[K]
            od = state.order_depths.get(sym)
            cur = state.position.get(sym, 0)

            if od is None or not od.buy_orders or not od.sell_orders:
                # missing book -> skip, do not pollute EMA
                continue

            best_bid = max(od.buy_orders.keys())
            best_ask = min(od.sell_orders.keys())
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid

            # bad-tick filter: huge mid jump vs typical scale
            last_mid = ss.get("last_mid")
            if (last_mid is not None and ss["abs_dev"] > 0
                    and abs(mid - last_mid) > JUMP_K * ss["abs_dev"]
                    and ss["n"] > WARMUP_TICKS):
                continue

            # ---- signal ----
            try:
                iv_hat = compute_iv_hat(S, K, TTE_days)
                if iv_hat <= 0:
                    iv_hat = IV_HAT_INTERCEPT
                bs_theo = bs_call_price(S, K, TTE_years, iv_hat)
                resid = mid - bs_theo
            except Exception:
                continue

            # ---- EMA update ----
            if ss["n"] == 0:
                ss["mu"] = resid
                ss["abs_dev"] = 0.0
                ss["recent_dev"] = 0.0
            else:
                ss["mu"] = ema_update(ss["mu"], resid, SPAN_LONG)
                dev = abs(resid - ss["mu"])
                ss["abs_dev"] = ema_update(ss["abs_dev"], dev, SPAN_LONG)
                ss["recent_dev"] = ema_update(ss["recent_dev"], dev, SPAN_SHORT)
            ss["n"] += 1
            ss["last_mid"] = mid

            # ---- decide target (warmup gates trading only, not EMA) ----
            if ss["n"] < WARMUP_TICKS:
                target = cur
            else:
                score = (resid - ss["mu"]) / max(ss["abs_dev"], EPS)
                target = decide_target(score, ss["recent_dev"], cur)

            # spread filter: skip ordering if too wide
            if spread > MAX_SPREAD and target != 0:
                target = cur

            # ---- emit orders ----
            target = max(-LIMIT, min(LIMIT, target))
            if target != cur:
                orders = target_to_orders(sym, target, cur, best_bid, best_ask)
                if orders:
                    result[sym] = orders

        return result, conversions, json.dumps(self._dump(strike_states))

    @staticmethod
    def _dump(strike_states: Dict[int, Dict]) -> Dict[str, Dict]:
        return {str(K): ss for K, ss in strike_states.items()}
