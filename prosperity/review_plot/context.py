"""ReviewContext: 把 dataio + fair + enrich + filters 串成一个对象，绘图层只依赖它。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

import polars as pl

from . import dataio, enrich, filters
from .fair import compute_all_fairs


@dataclass
class ReviewContext:
    submission_id: str
    products: list[str]
    ob_wide: pl.DataFrame
    ob_long: pl.DataFrame
    all_trades: pl.DataFrame
    my_trades: pl.DataFrame
    mkt_trades: pl.DataFrame
    fair_df: pl.DataFrame
    my_with_fair: pl.DataFrame
    position_df: pl.DataFrame
    pnl_by_product: pl.DataFrame
    pnl_total: pl.DataFrame
    meta: dict

    # 展示选项
    cross_fair_only: bool = False
    near_fair_tick: Optional[int] = 4
    show_delta_in_hover: bool = True
    position_limits: dict[str, int] = field(default_factory=dict)

    # ----------- factory -----------
    @classmethod
    def from_submission(
        cls,
        submission_id: str,
        my_trades_dir: str | Path = "data/my_trades",
        *,
        products: list[str] | None = None,
        ts_range: tuple[int | None, int | None] = (None, None),
        qty_filter: set[int] | None = None,
        edge_filter: Literal["pos", "neg", "zero", "nonneg"] | None = None,
        cross_fair_only: bool = False,
        near_fair_tick: Optional[int] = 4,
        show_delta_in_hover: bool = True,
        position_limits: dict[str, int] | None = None,
        fair_overrides: dict[str, Callable] | None = None,
    ) -> "ReviewContext":
        raw = dataio.load_submission(submission_id, my_trades_dir)

        if products is None:
            products = (
                raw.ob_wide["product"].unique().to_list()
                if not raw.ob_wide.is_empty()
                else []
            )

        ob_wide = raw.ob_wide.filter(pl.col("product").is_in(products)) if products else raw.ob_wide
        ob_long = raw.ob_long.filter(pl.col("product").is_in(products)) if products else raw.ob_long
        all_trades = (
            raw.all_trades.filter(pl.col("product").is_in(products)) if products else raw.all_trades
        )
        my_trades = all_trades.filter(pl.col("trade_type") != "market")
        mkt_trades = all_trades.filter(pl.col("trade_type") == "market")

        # fair
        fair_df = compute_all_fairs(ob_wide, products, overrides=fair_overrides)

        # 数量过滤
        if qty_filter is not None:
            my_trades = filters.filter_trades_by_qty(my_trades, qty_filter)
            mkt_trades = filters.filter_trades_by_qty(mkt_trades, qty_filter)
            ob_long = filters.filter_orders_by_qty(ob_long, qty_filter, abs_value=True)
            all_trades = pl.concat(
                [my_trades, mkt_trades], how="vertical_relaxed"
            ).sort(["product", "timestamp"]) if my_trades.height + mkt_trades.height else all_trades

        # 时间窗口过滤
        ts_min, ts_max = ts_range
        for _df_name in ("ob_wide", "ob_long", "all_trades", "my_trades", "mkt_trades", "fair_df"):
            locals_df = locals()[_df_name]
            locals()[_df_name] = filters.filter_by_timestamp(locals_df, ts_min, ts_max)
        # locals() 不保证更新 variable — 显式重赋
        ob_wide = filters.filter_by_timestamp(ob_wide, ts_min, ts_max)
        ob_long = filters.filter_by_timestamp(ob_long, ts_min, ts_max)
        all_trades = filters.filter_by_timestamp(all_trades, ts_min, ts_max)
        my_trades = filters.filter_by_timestamp(my_trades, ts_min, ts_max)
        mkt_trades = filters.filter_by_timestamp(mkt_trades, ts_min, ts_max)
        fair_df = filters.filter_by_timestamp(fair_df, ts_min, ts_max)

        # enrich
        my_with_fair = enrich.attach_fair_and_edge(my_trades, fair_df)

        if edge_filter is not None:
            my_with_fair = filters.filter_trades_by_edge(my_with_fair, edge_filter)
            # 同步 my_trades (通过 semi-join 与 my_with_fair 对齐)
            if my_with_fair.height:
                my_trades = my_trades.join(
                    my_with_fair.select(["timestamp", "product", "price", "quantity"]).unique(),
                    on=["timestamp", "product", "price", "quantity"],
                    how="semi",
                )

        # 标注 cross/near fair
        ob_long = filters.mark_cross_fair(ob_long, fair_df)
        if near_fair_tick is not None:
            ob_long = filters.mark_near_fair(ob_long, fair_df, tick=near_fair_tick)
        else:
            ob_long = ob_long.with_columns(pl.lit(False).alias("near_fair"))

        # delta for orders & mkt_trades
        ob_long = enrich.attach_delta_to_orders(
            ob_long.drop("fair") if "fair" in ob_long.columns else ob_long,
            fair_df,
        )
        mkt_trades = enrich.attach_delta_to_trades(mkt_trades, fair_df)

        position_df = enrich.compute_position_df(my_trades, products)
        pnl_by_product = enrich.compute_pnl_by_product(ob_wide)

        return cls(
            submission_id=submission_id,
            products=products,
            ob_wide=ob_wide,
            ob_long=ob_long,
            all_trades=all_trades,
            my_trades=my_trades,
            mkt_trades=mkt_trades,
            fair_df=fair_df,
            my_with_fair=my_with_fair,
            position_df=position_df,
            pnl_by_product=pnl_by_product,
            pnl_total=raw.pnl_total,
            meta=raw.meta,
            cross_fair_only=cross_fair_only,
            near_fair_tick=near_fair_tick,
            show_delta_in_hover=show_delta_in_hover,
            position_limits=position_limits or {},
        )

    # ----------- 便捷切片 -----------
    def for_product(self, product: str) -> "ReviewSlice":
        return ReviewSlice(ctx=self, product=product)


@dataclass
class ReviewSlice:
    ctx: ReviewContext
    product: str

    def ob_long(self) -> pl.DataFrame:
        return self.ctx.ob_long.filter(pl.col("product") == self.product)

    def fair(self) -> pl.DataFrame:
        return self.ctx.fair_df.filter(pl.col("product") == self.product).sort("timestamp")

    def my_buy(self) -> pl.DataFrame:
        return self.ctx.my_with_fair.filter(
            (pl.col("product") == self.product) & (pl.col("trade_type") == "my_buy")
        )

    def my_sell(self) -> pl.DataFrame:
        return self.ctx.my_with_fair.filter(
            (pl.col("product") == self.product) & (pl.col("trade_type") == "my_sell")
        )

    def mkt(self) -> pl.DataFrame:
        return self.ctx.mkt_trades.filter(pl.col("product") == self.product)

    def position(self) -> pl.DataFrame:
        return self.ctx.position_df.filter(pl.col("product") == self.product).sort("timestamp")

    def pnl(self) -> pl.DataFrame:
        return self.ctx.pnl_by_product.filter(pl.col("product") == self.product).sort("timestamp")
