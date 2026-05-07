"""Context: 把 dataio + enrich + fair 装在一起，绘图层只依赖它。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import polars as pl

from . import dataio, enrich
from .fair import compute_all_fairs


@dataclass
class Context:
    days: list[int]
    products: list[str]
    ob_wide: pl.DataFrame
    ob_long: pl.DataFrame
    trades: pl.DataFrame
    fair_df: pl.DataFrame

    voucher_symbols: list[str] = field(default_factory=lambda: list(dataio.VOUCHER_SYMBOLS))
    underlying: str = dataio.UNDERLYING

    @classmethod
    def from_data_dir(
        cls,
        data_dir: str | Path = "data",
        days: list[int] | None = None,
        fair_overrides: dict[str, Callable] | None = None,
        round_num: int = 3,
    ) -> "Context":
        raw = dataio.load_days(data_dir, days=days, round_num=round_num)

        # 依序附加派生列
        ob_wide = enrich.attach_spread(raw.ob_wide)
        ob_wide = enrich.attach_strike(ob_wide)
        ob_wide = enrich.attach_tte(ob_wide)
        ob_wide = enrich.attach_moneyness(ob_wide)

        ob_long = enrich.attach_strike(raw.ob_long)
        ob_long = enrich.attach_tte(ob_long)

        trades = enrich.attach_trade_flow(raw.trades)
        trades = enrich.attach_strike(trades)
        trades = enrich.attach_tte(trades)

        products = ob_wide["product"].unique().to_list() if not ob_wide.is_empty() else []
        overrides = dict(fair_overrides or {})
        if round_num == 5:
            from .fair import base as fair_base

            for p in products:
                overrides.setdefault(p, lambda ob, p=p: fair_base.round5_fair(ob, p))
        fair_df = compute_all_fairs(ob_wide, products, overrides=overrides)

        return cls(
            days=raw.days,
            products=products,
            ob_wide=ob_wide,
            ob_long=ob_long,
            trades=trades,
            fair_df=fair_df,
        )

    def vouchers(self, include: list[int] | None = None) -> list[str]:
        if include is None:
            return [v for v in self.voucher_symbols if v in self.products]
        wanted = {f"VEV_{k}" for k in include}
        return [v for v in self.voucher_symbols if v in wanted and v in self.products]

    def product_slice(self, product: str) -> pl.DataFrame:
        return self.ob_wide.filter(pl.col("product") == product).sort("global_ts")

    def voucher_panel(self) -> pl.DataFrame:
        """所有 voucher 的 ob_wide 切片；供 smile / grid 类图使用。"""
        return self.ob_wide.filter(pl.col("product").str.starts_with("VEV_")).sort(
            ["strike", "global_ts"]
        )
