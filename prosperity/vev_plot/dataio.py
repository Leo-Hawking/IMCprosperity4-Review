"""读取 Round 3 历史 csv 并合并成统一的 polars DataFrame。

输入文件:
  prices_round_3_day_{0,1,2}.csv —— 每 (day, timestamp, product) 一行的 orderbook 快照
  trades_round_3_day_{0,1,2}.csv —— 市场成交流水

约定:
  * 保留原始的 (day, timestamp) 两列，且引入 `global_ts = day * 1_000_000 + timestamp`
    作为跨天连续的 x 轴。
  * TTE 按官方规则: day 0 起 TTE=8d, day 1 起 TTE=7d, day 2 起 TTE=6d，
    round 3 提交当天起 TTE=5d。一天内 TTE 线性递减。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

VOUCHER_STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VOUCHER_SYMBOLS = [f"VEV_{k}" for k in VOUCHER_STRIKES]
UNDERLYING = "VELVETFRUIT_EXTRACT"
DELTA1_EXTRA = "HYDROGEL_PACK"
ALL_PRODUCTS = VOUCHER_SYMBOLS + [UNDERLYING, DELTA1_EXTRA]

DAY_START_TTE = {0: 8, 1: 7, 2: 6}   # days to expiry at the start of each historical day
SUBMISSION_START_TTE = 5              # round 3 live submission
TICKS_PER_DAY = 1_000_000             # timestamp ranges 0..1_000_000 per day


@dataclass
class RawData:
    ob_wide: pl.DataFrame
    ob_long: pl.DataFrame
    trades: pl.DataFrame
    days: list[int]


def _coalesce_int(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Float64, strict=False)


def load_days(
    data_dir: str | Path = "data",
    days: list[int] | None = None,
) -> RawData:
    data_dir = Path(data_dir)
    if days is None:
        days = sorted(
            int(p.stem.split("_")[-1])
            for p in data_dir.glob("prices_round_3_day_*.csv")
        )

    price_frames: list[pl.DataFrame] = []
    trade_frames: list[pl.DataFrame] = []
    for d in days:
        price_frames.append(_read_prices(data_dir / f"prices_round_3_day_{d}.csv"))
        tp = data_dir / f"trades_round_3_day_{d}.csv"
        if tp.exists():
            trade_frames.append(_read_trades(tp, d))

    ob_wide = (
        pl.concat(price_frames, how="vertical_relaxed")
        .sort(["product", "day", "timestamp"])
    )
    ob_wide = _attach_global_ts(ob_wide)
    ob_long = _wide_to_long(ob_wide)
    trades = (
        pl.concat(trade_frames, how="vertical_relaxed")
        .sort(["product", "day", "timestamp"])
        if trade_frames
        else pl.DataFrame(schema={"day": pl.Int64, "timestamp": pl.Int64, "product": pl.String,
                                   "price": pl.Float64, "quantity": pl.Int64, "global_ts": pl.Int64})
    )
    if trade_frames:
        trades = _attach_global_ts(trades)

    return RawData(ob_wide=ob_wide, ob_long=ob_long, trades=trades, days=days)


def _read_prices(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path, separator=";")
    numeric_cols = [c for c in df.columns
                    if c.startswith(("bid_price_", "bid_volume_", "ask_price_", "ask_volume_"))
                    or c in {"mid_price", "profit_and_loss"}]
    df = df.with_columns([_coalesce_int(c).alias(c) for c in numeric_cols])
    df = df.with_columns(
        pl.col("day").cast(pl.Int64, strict=False),
        pl.col("timestamp").cast(pl.Int64, strict=False),
    )
    return df


def _read_trades(path: Path, day: int) -> pl.DataFrame:
    df = pl.read_csv(path, separator=";")
    if "symbol" in df.columns and "product" not in df.columns:
        df = df.rename({"symbol": "product"})
    if "day" not in df.columns:
        df = df.with_columns(pl.lit(day, dtype=pl.Int64).alias("day"))
    df = df.with_columns(
        pl.col("timestamp").cast(pl.Int64, strict=False),
        pl.col("price").cast(pl.Float64, strict=False),
        pl.col("quantity").cast(pl.Int64, strict=False),
    )
    return df


def _attach_global_ts(df: pl.DataFrame) -> pl.DataFrame:
    if "day" not in df.columns or "timestamp" not in df.columns:
        return df
    return df.with_columns(
        (pl.col("day").cast(pl.Int64) * TICKS_PER_DAY + pl.col("timestamp").cast(pl.Int64))
        .alias("global_ts")
    )


def _wide_to_long(ob_wide: pl.DataFrame) -> pl.DataFrame:
    if ob_wide.is_empty():
        return pl.DataFrame()
    pieces: list[pl.DataFrame] = []
    for side in ("bid", "ask"):
        for level in range(1, 4):
            pc, vc = f"{side}_price_{level}", f"{side}_volume_{level}"
            if pc not in ob_wide.columns:
                continue
            pieces.append(
                ob_wide.select([
                    "day", "timestamp", "global_ts", "product",
                    pl.col(pc).alias("price"),
                    pl.col(vc).alias("volume"),
                    pl.lit(side).alias("side"),
                    pl.lit(level).alias("level"),
                    "mid_price",
                ])
            )
    return (
        pl.concat(pieces, how="vertical_relaxed")
        .filter(pl.col("price").is_not_null())
        .sort(["product", "global_ts", "side", "level"])
    )
