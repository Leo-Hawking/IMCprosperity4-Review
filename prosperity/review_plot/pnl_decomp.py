"""按两种口径分解 PnL：accounting (cash + mtm) 与 trade-quality (edge + drift)。

两种分解都满足 total = compA + compB，但 compA/compB 的语义完全不同：

  total[t] = Σ_i signed_qty_i · (fair[t] - price_i)
           = -Σ price·signed_qty            ←── realized_cash (做完后剩多少现金)
             + position[t] · fair[t]        ←── mtm           (持仓被 mark)
           = Σ signed_qty · (fair_at_trade - price)
                                            ←── edge          (成交时刻的做市质量)
             + Σ signed_qty · (fair[t] - fair_at_trade)
                                            ←── drift         (持仓时段 fair 自身漂移)

为什么要两种：
- cash + mtm 是 IMC profit_and_loss 严格使用的会计口径，但 cash 会因每笔
  买/卖瞬时正负交替乱飘，看不出「做市做得好不好」。
- edge + drift 把「成交那一瞬间的优势」单独抽出：纯被动挂单（不跨 spread）
  的 edge 应该单调上升；drift 反映持仓在 fair 上的浮盈/浮亏，做市目标
  是 drift ≈ 0（仓位回到 0 让 drift 兑现）。

fair 选择:
    fair_source='mid'           → ob_wide.mid_price (与 activity log 严格对齐)
    fair_source='round5_walmid' → round5 规则 (max-vol wall_mid + lone-close 修正)
    fair_source=callable(ctx, product, ts_master) -> ndarray  → 自定义

切换 fair 后 realized 不变 (cash 是事实)，但 mtm / edge / drift 均会变，
因为它们都依赖 fair。
"""
from __future__ import annotations

import collections
from typing import Callable, Iterable, Optional, Union

import numpy as np
import polars as pl


# round 5 自然分组（每组 5 个产品）
R5_GROUPS: dict[str, list[str]] = {
    "PEBBLES": [
        "PEBBLES_XS", "PEBBLES_S", "PEBBLES_M", "PEBBLES_L", "PEBBLES_XL",
    ],
    "GALAXY_SOUNDS": [
        "GALAXY_SOUNDS_DARK_MATTER", "GALAXY_SOUNDS_BLACK_HOLES",
        "GALAXY_SOUNDS_PLANETARY_RINGS", "GALAXY_SOUNDS_SOLAR_WINDS",
        "GALAXY_SOUNDS_SOLAR_FLAMES",
    ],
    "SLEEP_POD": [
        "SLEEP_POD_SUEDE", "SLEEP_POD_LAMB_WOOL", "SLEEP_POD_POLYESTER",
        "SLEEP_POD_NYLON", "SLEEP_POD_COTTON",
    ],
    "MICROCHIP": [
        "MICROCHIP_CIRCLE", "MICROCHIP_OVAL", "MICROCHIP_SQUARE",
        "MICROCHIP_RECTANGLE", "MICROCHIP_TRIANGLE",
    ],
    "ROBOT": [
        "ROBOT_VACUUMING", "ROBOT_MOPPING", "ROBOT_DISHES",
        "ROBOT_LAUNDRY", "ROBOT_IRONING",
    ],
    "UV_VISOR": [
        "UV_VISOR_YELLOW", "UV_VISOR_AMBER", "UV_VISOR_ORANGE",
        "UV_VISOR_RED", "UV_VISOR_MAGENTA",
    ],
    "TRANSLATOR": [
        "TRANSLATOR_SPACE_GRAY", "TRANSLATOR_ASTRO_BLACK",
        "TRANSLATOR_ECLIPSE_CHARCOAL", "TRANSLATOR_GRAPHITE_MIST",
        "TRANSLATOR_VOID_BLUE",
    ],
    "PANEL": [
        "PANEL_1X2", "PANEL_2X2", "PANEL_1X4", "PANEL_2X4", "PANEL_4X4",
    ],
    "OXYGEN_SHAKE": [
        "OXYGEN_SHAKE_MORNING_BREATH", "OXYGEN_SHAKE_EVENING_BREATH",
        "OXYGEN_SHAKE_MINT", "OXYGEN_SHAKE_CHOCOLATE", "OXYGEN_SHAKE_GARLIC",
    ],
    "SNACKPACK": [
        "SNACKPACK_CHOCOLATE", "SNACKPACK_VANILLA", "SNACKPACK_PISTACHIO",
        "SNACKPACK_STRAWBERRY", "SNACKPACK_RASPBERRY",
    ],
}


# 缓存：(id(ctx), fair_source_key) -> {ts_master, arrs[product] = {realized, mtm}}
_DECOMP_CACHE: dict[tuple, dict] = {}


# ─── round 5 fair 实现 ─────────────────────────────────────────────────────
def _row_round5_fair(row: dict) -> Optional[float]:
    """单行计算 round5 fair: wall_mid (max-vol bid + max-vol ask)/2，
    若有且仅有 1 个挂单 |p - wall_mid| < 1，fair = 该挂单价；否则 wall_mid - 0.5。
    book 缺一边返回 None（由 forward-fill 兜底）。
    """
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for i in (1, 2, 3):
        p = row.get(f"bid_price_{i}")
        v = row.get(f"bid_volume_{i}")
        if p is not None and v is not None and v > 0:
            bids.append((float(p), float(v)))
        p = row.get(f"ask_price_{i}")
        v = row.get(f"ask_volume_{i}")
        if p is not None and v is not None and v > 0:
            asks.append((float(p), float(v)))
    if not bids or not asks:
        return None
    bid_wall = max(bids, key=lambda x: (x[1], x[0]))[0]
    ask_wall = max(asks, key=lambda x: (x[1], -x[0]))[0]
    wall_mid = (bid_wall + ask_wall) / 2.0
    close = [p for p, _ in bids if abs(p - wall_mid) < 1] + \
            [p for p, _ in asks if abs(p - wall_mid) < 1]
    if len(close) == 1:
        return float(close[0])
    return wall_mid - 0.5


def _round5_fair_for_product(ctx, product: str, ts_master: list) -> np.ndarray:
    """把 round5 fair 算到 master ts 上（前向填充）。"""
    n = len(ts_master)
    out = np.zeros(n)
    last = 0.0

    prod_ob = (ctx.ob_wide.filter(pl.col("product") == product)
                  .sort("timestamp"))
    if prod_ob.is_empty():
        return out

    fair_dict: dict[int, float] = {}
    for r in prod_ob.iter_rows(named=True):
        f = _row_round5_fair(r)
        if f is not None:
            fair_dict[int(r["timestamp"])] = f

    for i, t in enumerate(ts_master):
        v = fair_dict.get(t)
        if v is not None:
            last = v
        out[i] = last
    return out


def _mid_fair_for_product(ctx, product: str, ts_master: list) -> np.ndarray:
    """ob_wide.mid_price，前向填充 0/null。"""
    n = len(ts_master)
    out = np.zeros(n)
    last = 0.0

    prod_ob = (ctx.ob_wide.filter(pl.col("product") == product)
                  .sort("timestamp")
                  .select(["timestamp", "mid_price"]))
    if prod_ob.is_empty():
        return out

    mid_dict = dict(zip(prod_ob["timestamp"].to_list(),
                        prod_ob["mid_price"].to_list()))
    for i, t in enumerate(ts_master):
        v = mid_dict.get(t)
        if v is not None and v > 0:
            last = float(v)
        out[i] = last
    return out


_FAIR_SOURCES: dict[str, Callable] = {
    "mid": _mid_fair_for_product,
    "round5_walmid": _round5_fair_for_product,
}


# ─── 主入口 ────────────────────────────────────────────────────────────────
def _per_product_arrays(ctx, fair_source: Union[str, Callable] = "mid") -> dict:
    """为 ctx 一次性算好 master ts + 每个 product 的 (realized, mtm) ndarray。

    fair_source: 'mid' | 'round5_walmid' | callable(ctx, product, ts_master) -> ndarray
    """
    src_key = fair_source if isinstance(fair_source, str) else f"callable:{id(fair_source)}"
    cache_key = (id(ctx), src_key)
    cached = _DECOMP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if isinstance(fair_source, str):
        fair_fn = _FAIR_SOURCES.get(fair_source)
        if fair_fn is None:
            raise ValueError(f"unknown fair_source: {fair_source}; "
                             f"choices: {list(_FAIR_SOURCES)}")
    else:
        fair_fn = fair_source

    ob = ctx.ob_wide
    if ob.is_empty():
        out = {"ts_master": [], "arrs": {}}
        _DECOMP_CACHE[cache_key] = out
        return out

    ts_master = sorted(ob["timestamp"].unique().to_list())
    n = len(ts_master)

    products = ob["product"].unique().to_list()
    arrs: dict[str, dict] = {}

    pos_by_prod = ctx.position_df.partition_by("product", as_dict=True)
    trades_by_prod = (ctx.my_trades.partition_by("product", as_dict=True)
                      if not ctx.my_trades.is_empty() else {})

    for product in products:
        # fair series
        fair = fair_fn(ctx, product, ts_master)

        # position series
        positions = np.zeros(n, dtype=float)
        last_pos = 0.0
        pos_df = pos_by_prod.get((product,))
        if pos_df is None:
            pos_df = pos_by_prod.get(product)
        if pos_df is not None and not pos_df.is_empty():
            pos_df = pos_df.sort("timestamp")
            pos_ts = pos_df["timestamp"].to_list()
            pos_val = pos_df["position"].to_list()
            pj = 0
            for i, t in enumerate(ts_master):
                while pj < len(pos_ts) and pos_ts[pj] <= t:
                    last_pos = float(pos_val[pj])
                    pj += 1
                positions[i] = last_pos

        # realized cash + edge captured at trade time
        realized = np.zeros(n, dtype=float)
        edge = np.zeros(n, dtype=float)
        tr_df = trades_by_prod.get((product,))
        if tr_df is None:
            tr_df = trades_by_prod.get(product)
        if tr_df is not None and not tr_df.is_empty():
            cash_at_ts: dict[int, float] = collections.defaultdict(float)
            edge_at_ts: dict[int, float] = collections.defaultdict(float)
            ts_to_idx = {t: i for i, t in enumerate(ts_master)}
            for r in tr_df.iter_rows(named=True):
                qty = float(r["quantity"]); px = float(r["price"])
                ts = int(r["timestamp"])
                signed = qty if r["trade_type"] == "my_buy" else -qty
                cash_at_ts[ts] -= px * signed  # buy: signed=+qty → cash -= px·qty
                idx = ts_to_idx.get(ts)
                if idx is not None:
                    fair_at_trade = float(fair[idx])
                    edge_at_ts[ts] += signed * (fair_at_trade - px)
            cum_cash = 0.0
            cum_edge = 0.0
            for i, t in enumerate(ts_master):
                cum_cash += cash_at_ts.get(t, 0.0)
                cum_edge += edge_at_ts.get(t, 0.0)
                realized[i] = cum_cash
                edge[i] = cum_edge

        mtm = positions * fair
        total = realized + mtm  # = edge + drift
        drift = total - edge

        arrs[product] = {
            "realized": realized,
            "mtm": mtm,
            "edge": edge,
            "drift": drift,
            "total": total,
        }

    out = {"ts_master": ts_master, "arrs": arrs}
    _DECOMP_CACHE[cache_key] = out
    return out


def compute_decomp(ctx, products: Iterable[str],
                   fair_source: Union[str, Callable] = "mid") -> pl.DataFrame:
    """给定 product 列表，把 PnL 同时按两种口径分解到时间序列。

    返回: DataFrame[timestamp, realized, mtm, edge, drift, total]
      realized + mtm = total          (accounting / cash 口径)
      edge     + drift = total        (trade-quality / fair 口径)

    fair_source:
      'mid' (默认): ob_wide.mid_price，total 与 activity log 严格一致
      'round5_walmid': round5 fair（wall_mid + 孤立挂单修正）
      callable(ctx, product, ts_master) -> ndarray: 自定义
    """
    products = list(products)
    cache = _per_product_arrays(ctx, fair_source)
    ts = cache["ts_master"]
    arrs = cache["arrs"]
    if not ts:
        return pl.DataFrame(schema={
            "timestamp": pl.Int64, "realized": pl.Float64, "mtm": pl.Float64,
            "edge": pl.Float64, "drift": pl.Float64, "total": pl.Float64,
        })

    n = len(ts)
    realized = np.zeros(n); mtm = np.zeros(n)
    edge = np.zeros(n); drift = np.zeros(n)
    for p in products:
        a = arrs.get(p)
        if a is None:
            continue
        realized += a["realized"]
        mtm += a["mtm"]
        edge += a["edge"]
        drift += a["drift"]

    return pl.DataFrame({
        "timestamp": ts,
        "realized": realized,
        "mtm": mtm,
        "edge": edge,
        "drift": drift,
        "total": realized + mtm,
    })


def clear_cache() -> None:
    _DECOMP_CACHE.clear()
