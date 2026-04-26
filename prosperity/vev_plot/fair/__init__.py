"""Fair value dispatcher。

Round 3 初期：还没想好每个产品的 fair 怎么算，所以这里默认返回 `wall_mid`
或 csv 自带的 `mid_price`。保留 `PRODUCT_FAIR_REGISTRY` 以便后续每个产品
独立加一个文件（如 `VEV_5000.py` 里 implement `compute_fair(ob_wide)`）。
"""
from __future__ import annotations

from typing import Callable

import polars as pl

from . import base

PRODUCT_FAIR_REGISTRY: dict[str, Callable[[pl.DataFrame], pl.DataFrame]] = {
    # 'VEV_5000': lambda ob: VEV_5000.compute_fair(ob),
    # 暂时为空，所有产品都走 default_fair
}


def default_fair(ob_wide: pl.DataFrame, product: str) -> pl.DataFrame:
    return base.wall_mid(ob_wide, product)


def compute_all_fairs(
    ob_wide: pl.DataFrame,
    products: list[str] | None = None,
    overrides: dict[str, Callable] | None = None,
) -> pl.DataFrame:
    """对每个产品调用其 compute_fair，产出统一 fair_df。"""
    if ob_wide.is_empty():
        return pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64})

    if products is None:
        products = ob_wide["product"].unique().to_list()

    overrides = overrides or {}
    pieces: list[pl.DataFrame] = []
    for p in products:
        fn = overrides.get(p) or PRODUCT_FAIR_REGISTRY.get(p)
        if fn is not None:
            fair = fn(ob_wide)
        else:
            fair = default_fair(ob_wide, p)
        if not fair.is_empty():
            pieces.append(fair)
    if not pieces:
        return pl.DataFrame()
    return pl.concat(pieces, how="vertical_relaxed").sort(["product", "timestamp"])
