"""Fair 计算的公共工具。"""
from __future__ import annotations

from typing import Optional

import polars as pl


def pick_max_vol_price(
    row: dict,
    side: str,
    vol_threshold: float = 0.0,
) -> Optional[float]:
    """该侧 (bid/ask) 三档里成交量绝对值最大且 > vol_threshold 的价。平手时 bid 取高价、ask 取低价。"""
    cand: list[tuple[float, float]] = []
    for level in range(1, 4):
        p = row.get(f"{side}_price_{level}")
        v = row.get(f"{side}_volume_{level}")
        if p is None or v is None:
            continue
        v_abs = abs(float(v))
        if v_abs > vol_threshold:
            cand.append((v_abs, float(p)))
    if not cand:
        return None
    max_vol = max(v for v, _ in cand)
    top = [p for v, p in cand if v == max_vol]
    return max(top) if side == "bid" else min(top)


def compute_wall_mid_fair(ob_wide: pl.DataFrame, product: str | None = None) -> pl.DataFrame:
    """通用 fallback: 取 bid/ask 量最大档位的中点。无阈值要求。

    返回: DataFrame[timestamp, product, fair]
    """
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})

    df = ob_wide
    if product is not None:
        df = df.filter(pl.col("product") == product)

    rows = []
    for row in df.iter_rows(named=True):
        bid_p = pick_max_vol_price(row, "bid", vol_threshold=0.0)
        ask_p = pick_max_vol_price(row, "ask", vol_threshold=0.0)
        if bid_p is not None and ask_p is not None:
            fair = (bid_p + ask_p) / 2.0
        elif row.get("mid_price") is not None:
            fair = float(row["mid_price"])
        else:
            fair = None
        rows.append({
            "timestamp": int(row["timestamp"]),
            "product": row["product"],
            "fair": fair,
        })
    return pl.DataFrame(rows, schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})


def forward_fill_two_sides(
    ob_wide: pl.DataFrame,
    product: str,
    vol_threshold: float = 20.0,
    mid_fallback: bool = True,
) -> pl.DataFrame:
    """遍历 product 的快照，对 max-vol > threshold 的两侧分别 forward-fill。

    - 双侧均有（当下或历史）→ fair = (bid + ask) / 2
    - 最早期全缺 → 若 mid_fallback=True 用 mid_price 兜底，否则 fair = None
    """
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})

    sub = ob_wide.filter(pl.col("product") == product).sort("timestamp")
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    rows = []
    for row in sub.iter_rows(named=True):
        bid_px = pick_max_vol_price(row, "bid", vol_threshold=vol_threshold)
        ask_px = pick_max_vol_price(row, "ask", vol_threshold=vol_threshold)
        use_bid = bid_px if bid_px is not None else last_bid
        use_ask = ask_px if ask_px is not None else last_ask
        if bid_px is not None:
            last_bid = bid_px
        if ask_px is not None:
            last_ask = ask_px

        if use_bid is not None and use_ask is not None:
            fair = (use_bid + use_ask) / 2.0
        elif mid_fallback and row.get("mid_price") is not None:
            fair = float(row["mid_price"])
        else:
            fair = None
        rows.append({
            "timestamp": int(row["timestamp"]),
            "product": product,
            "fair": fair,
        })
    return pl.DataFrame(rows, schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})
