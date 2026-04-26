"""CSV/parquet 读写，统一数据格式。"""

import polars as pl
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "bt"
PROCESSED_DIR = DATA_DIR / "processed"


def _prices_schema_overrides() -> dict[str, pl.DataType]:
    """Prices CSV 的显式 schema，避免稀疏档位被推断成字符串。"""
    schema: dict[str, pl.DataType] = {
        "day": pl.Int64,
        "timestamp": pl.Int64,
        "product": pl.String,
        "mid_price": pl.Float64,
        "profit_and_loss": pl.Float64,
    }
    for side in ("bid", "ask"):
        for level in range(1, 4):
            schema[f"{side}_price_{level}"] = pl.Int64
            schema[f"{side}_volume_{level}"] = pl.Int64
    return schema


def load_prices(round_num: int, day: int) -> pl.DataFrame:
    """加载 prices CSV，返回长格式 DataFrame（每行一个 bid/ask 档位）。"""
    path = RAW_DIR / f"prices_round_{round_num}_day_{day}.csv"
    raw = pl.read_csv(path, separator=";", schema_overrides=_prices_schema_overrides())

    # 宽格式 -> 长格式：每个 bid/ask 档位一行
    rows = []
    for col_set in [("bid", 3), ("ask", 3)]:
        side = col_set[0]
        for level in range(1, col_set[1] + 1):
            price_col = f"{side}_price_{level}"
            vol_col = f"{side}_volume_{level}"
            if price_col in raw.columns:
                subset = raw.select([
                    "day", "timestamp", "product",
                    pl.col(price_col).alias("price"),
                    pl.col(vol_col).alias("volume"),
                    pl.lit(side).alias("side"),
                    pl.lit(level).alias("level"),
                    "mid_price",
                ])
                rows.append(subset)

    long = pl.concat(rows).filter(pl.col("price").is_not_null()).sort(["timestamp", "product", "side", "level"])
    return long


def load_prices_wide(round_num: int, day: int) -> pl.DataFrame:
    """加载 prices CSV，保持宽格式原样返回。"""
    path = RAW_DIR / f"prices_round_{round_num}_day_{day}.csv"
    return pl.read_csv(path, separator=";", schema_overrides=_prices_schema_overrides())


def load_trades(round_num: int, day: int) -> pl.DataFrame:
    """加载 trades CSV。"""
    path = RAW_DIR / f"trades_round_{round_num}_day_{day}.csv"
    df = pl.read_csv(path, separator=";")
    # 统一列名
    rename_map = {}
    if "symbol" in df.columns:
        rename_map["symbol"] = "product"
    if rename_map:
        df = df.rename(rename_map)
    return df


def available_files() -> list[Path]:
    """列出 raw 目录下所有数据文件。"""
    return sorted(RAW_DIR.glob("*.csv"))
