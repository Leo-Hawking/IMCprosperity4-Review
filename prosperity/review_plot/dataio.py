"""解析提交 / 回测日志为 polars DataFrame。

输入:
  - `<dir>/<id>.log`  (JSON containing activitiesLog + tradeHistory)
  - `<dir>/<id>.json` (JSON containing graphLog + profit + positions)

输出:
  - ob_wide   : 每条 (timestamp, product) 一行的 orderbook 快照
  - ob_long   : 每档 (timestamp, product, side, level) 展开一行
  - all_trades: 全部成交 (含 trade_type 分类)
  - my_trades / mkt_trades: 按 trade_type 拆分
  - pnl_total : graphLog CSV -> DataFrame (可为空)
  - meta      : round / status / profit / positions
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import polars as pl

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

_NUMERIC_FIXED = {"timestamp", "day", "mid_price", "profit_and_loss"}


@dataclass
class RawSubmission:
    submission_id: str
    ob_wide: pl.DataFrame
    ob_long: pl.DataFrame
    all_trades: pl.DataFrame
    my_trades: pl.DataFrame
    mkt_trades: pl.DataFrame
    pnl_total: pl.DataFrame
    meta: dict


def load_submission(submission_id: str, my_trades_dir: str | Path) -> RawSubmission:
    my_trades_dir = Path(my_trades_dir)
    log_path = my_trades_dir / f"{submission_id}.log"
    json_path = my_trades_dir / f"{submission_id}.json"

    with open(log_path) as f:
        raw = f.read()

    # 自动识别两种格式：
    #   1) 线上提交 my_trades/*.log — 纯 JSON {activitiesLog, tradeHistory, ...}
    #   2) 回测 backtest/runs/*.log — visualizer 三段式文本
    if raw.lstrip().startswith("{") and "activitiesLog" in raw[:200]:
        log_data = json.loads(raw)
    else:
        log_data = _parse_visualizer_log(raw)

    if json_path.exists():
        with open(json_path) as f:
            json_data = json.load(f)
    else:
        json_data = {}

    ob_wide = _parse_activities(log_data.get("activitiesLog", ""))
    ob_long = _wide_to_long(ob_wide)
    all_trades = _parse_trades(log_data.get("tradeHistory", []))
    my_trades = all_trades.filter(pl.col("trade_type") != "market")
    mkt_trades = all_trades.filter(pl.col("trade_type") == "market")

    graph_log = json_data.get("graphLog", "")
    pnl_total = _parse_graph_log(graph_log)

    meta = {
        "round": json_data.get("round"),
        "status": json_data.get("status"),
        "profit": json_data.get("profit"),
        "positions": json_data.get("positions"),
    }

    return RawSubmission(
        submission_id=submission_id,
        ob_wide=ob_wide,
        ob_long=ob_long,
        all_trades=all_trades,
        my_trades=my_trades,
        mkt_trades=mkt_trades,
        pnl_total=pnl_total,
        meta=meta,
    )


def _parse_activities(activities_csv: str) -> pl.DataFrame:
    if not activities_csv:
        return pl.DataFrame()
    df = pl.read_csv(io.StringIO(activities_csv), separator=";")
    numeric_cols = [
        c
        for c in df.columns
        if c in _NUMERIC_FIXED
        or c.startswith("bid_price_")
        or c.startswith("ask_price_")
        or c.startswith("bid_volume_")
        or c.startswith("ask_volume_")
    ]
    if numeric_cols:
        df = df.with_columns(
            [pl.col(c).cast(pl.Float64, strict=False).alias(c) for c in numeric_cols]
        )
        if "timestamp" in df.columns:
            df = df.with_columns(pl.col("timestamp").cast(pl.Int64, strict=False))
    return df.sort(["product", "timestamp"]) if "product" in df.columns else df


def _wide_to_long(ob_wide: pl.DataFrame) -> pl.DataFrame:
    if ob_wide.is_empty():
        return pl.DataFrame(
            schema={
                "day": pl.Float64,
                "timestamp": pl.Int64,
                "product": pl.String,
                "price": pl.Float64,
                "volume": pl.Float64,
                "side": pl.String,
                "level": pl.Int64,
                "mid_price": pl.Float64,
                "profit_and_loss": pl.Float64,
            }
        )

    pieces: list[pl.DataFrame] = []
    for side in ("bid", "ask"):
        for level in range(1, 4):
            pc, vc = f"{side}_price_{level}", f"{side}_volume_{level}"
            if pc not in ob_wide.columns:
                continue
            pieces.append(
                ob_wide.select(
                    [
                        "day",
                        "timestamp",
                        "product",
                        pl.col(pc).cast(pl.Float64, strict=False).alias("price"),
                        pl.col(vc).cast(pl.Float64, strict=False).alias("volume"),
                        pl.lit(side).alias("side"),
                        pl.lit(level).alias("level"),
                        "mid_price",
                        "profit_and_loss",
                    ]
                )
            )
    if not pieces:
        return pl.DataFrame()
    return (
        pl.concat(pieces, how="vertical_relaxed")
        .filter(pl.col("price").is_not_null())
        .sort(["product", "timestamp", "side", "level"])
    )


def _parse_trades(trade_history: list[dict]) -> pl.DataFrame:
    schema = {
        "product": pl.String,
        "price": pl.Float64,
        "quantity": pl.Int64,
        "buyer": pl.String,
        "seller": pl.String,
        "timestamp": pl.Int64,
    }
    if not trade_history:
        df = pl.DataFrame(schema=schema)
    else:
        df = pl.DataFrame(trade_history)
        if "symbol" in df.columns and "product" not in df.columns:
            df = df.rename({"symbol": "product"})
        for col_name, dtype in schema.items():
            if col_name not in df.columns:
                df = df.with_columns(pl.lit(None, dtype=dtype).alias(col_name))

    df = df.with_columns(
        pl.when(pl.col("buyer") == "SUBMISSION")
        .then(pl.lit("my_buy"))
        .when(pl.col("seller") == "SUBMISSION")
        .then(pl.lit("my_sell"))
        .otherwise(pl.lit("market"))
        .alias("trade_type")
    )
    if "product" in df.columns and "timestamp" in df.columns:
        df = df.sort(["product", "timestamp"])
    return df


def _parse_visualizer_log(raw: str) -> dict:
    """解析 prosperity3bt / 官方 submission 环境的三段式输出。

    段落标记: "Sandbox logs:" / "Activities log:" / "Trade History:"
    """
    markers = ["Sandbox logs:", "Activities log:", "Trade History:"]
    pos = [raw.find(m) for m in markers]
    # 把后续段的起点或文件尾作为结束
    end = [
        pos[1] if pos[1] >= 0 else (pos[2] if pos[2] >= 0 else len(raw)),
        pos[2] if pos[2] >= 0 else len(raw),
        len(raw),
    ]

    def seg(i: int) -> str:
        if pos[i] < 0:
            return ""
        start = pos[i] + len(markers[i])
        return raw[start:end[i]].strip()

    sandbox_block = seg(0)
    activities_block = seg(1)
    trades_block = seg(2)

    trade_history = []
    if trades_block:
        # 回测输出偶尔带非法的末尾逗号 (e.g. `"quantity": 10,\n}`)，先清洗再解析
        cleaned = _TRAILING_COMMA_RE.sub(r"\1", trades_block)
        try:
            trade_history = json.loads(cleaned)
        except json.JSONDecodeError:
            trade_history = []

    return {
        "sandboxLog": sandbox_block,
        "activitiesLog": activities_block,
        "tradeHistory": trade_history,
    }


def _parse_graph_log(graph_log: str) -> pl.DataFrame:
    if not graph_log:
        return pl.DataFrame()
    try:
        return pl.read_csv(io.StringIO(graph_log), separator=";")
    except Exception:
        return pl.DataFrame()
