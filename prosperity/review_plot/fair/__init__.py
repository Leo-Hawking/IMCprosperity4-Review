"""Fair 注册表与分派。

新增产品: 在本目录下写 `<PRODUCT>.py`，实现 `compute_fair(ob_wide, **params)` 并
在 `PRODUCT_FAIR_REGISTRY` 中注册。缺省产品走 `base.compute_wall_mid_fair` 兜底。
"""
from __future__ import annotations

from typing import Callable

import polars as pl

from . import ASH_COATED_OSMIUM, INTARIAN_PEPPER_ROOT
from .base import compute_wall_mid_fair

PRODUCT_FAIR_REGISTRY: dict[str, Callable[..., pl.DataFrame]] = {
    ASH_COATED_OSMIUM.PRODUCT: ASH_COATED_OSMIUM.compute_fair,
    INTARIAN_PEPPER_ROOT.PRODUCT: INTARIAN_PEPPER_ROOT.compute_fair,
}


def get_fair(
    product: str,
    ob_wide: pl.DataFrame,
    overrides: dict[str, Callable[..., pl.DataFrame]] | None = None,
    **kwargs,
) -> pl.DataFrame:
    registry = dict(PRODUCT_FAIR_REGISTRY)
    if overrides:
        registry.update(overrides)
    fn = registry.get(product)
    if fn is None:
        return compute_wall_mid_fair(ob_wide, product=product)
    return fn(ob_wide, **kwargs)


def compute_all_fairs(
    ob_wide: pl.DataFrame,
    products: list[str],
    overrides: dict[str, Callable[..., pl.DataFrame]] | None = None,
) -> pl.DataFrame:
    """对每个产品调用对应 compute_fair 并拼接。"""
    pieces: list[pl.DataFrame] = []
    for p in products:
        df = get_fair(p, ob_wide, overrides=overrides)
        if df.height:
            pieces.append(df)
    if not pieces:
        return pl.DataFrame(
            schema={"timestamp": pl.Int64, "product": pl.String, "fair": pl.Float64}
        )
    return pl.concat(pieces, how="vertical_relaxed").sort(["product", "timestamp"])
