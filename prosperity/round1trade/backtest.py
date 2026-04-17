"""
Custom backtest engine for ASH_COATED_OSMIUM strategy v2.

Per §1.2 市场微观结构规则:
  1. 仅最优价可成交。
  2. 无排队，同价对方优先 → 我方需严格更优才成交。
  3. 对方行为与市场状态无关。
  4. 每个 tick 未成交限价单自动清空。

Per-tick sequence:
  (a) Load market book snapshot at t (from prices CSV).
  (b) Our take orders cross and consume book liquidity (best-price-first).
  (c) Remaining become resting limit orders (clear at tick end).
  (d) Bot trades at t (from trades CSV): if bot price strictly crosses our
      resting quote → fill at our quote, size = min(our_qty, bot_qty).
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

try:
    from datamodel import Order, OrderDepth, TradingState, Trade, Listing, Observation
except ImportError:
    from prosperity4bt.datamodel import (
        Order, OrderDepth, TradingState, Trade, Listing, Observation,
    )


DATA_DIR = Path("/Users/leoliu/imc/prosperity/data/raw")


# --- Data loading -------------------------------------------------------------

def _cast_numeric(df: pl.DataFrame) -> pl.DataFrame:
    # Order-book level columns are integers but CSV may carry them as strings
    # due to nulls in higher levels. mid_price stays float (half-integers).
    level_cols = [
        f"{side}_{kind}_{i}"
        for side in ("bid", "ask")
        for kind in ("price", "volume")
        for i in (1, 2, 3)
    ]
    present = [c for c in level_cols if c in df.columns]
    return df.with_columns([
        pl.col(c).cast(pl.Float64, strict=False).cast(pl.Int64, strict=False)
        for c in present
    ])


def load_prices(day: int, product: str, round_num: int = 1) -> pl.DataFrame:
    path = DATA_DIR / f"prices_round_{round_num}_day_{day}.csv"
    df = pl.read_csv(path, separator=";", infer_schema_length=10000)
    df = df.filter(pl.col("product") == product).sort("timestamp")
    return _cast_numeric(df)


def load_trades(day: int, product: str, round_num: int = 1) -> pl.DataFrame:
    path = DATA_DIR / f"trades_round_{round_num}_day_{day}.csv"
    df = pl.read_csv(path, separator=";", infer_schema_length=10000)
    return df.filter(pl.col("symbol") == product).sort("timestamp")


def _build_order_depth(row: dict) -> OrderDepth:
    od = OrderDepth()
    od.buy_orders = {}
    od.sell_orders = {}
    for i in (1, 2, 3):
        p = row.get(f"bid_price_{i}")
        v = row.get(f"bid_volume_{i}")
        if p is not None and v is not None and v != 0:
            od.buy_orders[int(p)] = int(v)
        p = row.get(f"ask_price_{i}")
        v = row.get(f"ask_volume_{i}")
        if p is not None and v is not None and v != 0:
            od.sell_orders[int(p)] = -abs(int(v))
    return od


# --- Simulation ---------------------------------------------------------------

def simulate(trader, product: str, day: int = 0, round_num: int = 1,
             record_memory: bool = False) -> dict:
    prices = load_prices(day, product, round_num)
    trades = load_trades(day, product, round_num)

    trades_map: dict[int, list[tuple[float, int]]] = {}
    for row in trades.iter_rows(named=True):
        trades_map.setdefault(int(row["timestamp"]), []).append(
            (float(row["price"]), int(row["quantity"]))
        )

    listing = Listing(product, product, 1)
    empty_obs = Observation({}, {})

    records: list[dict] = []
    memory_trace: list[dict] = []
    memory_str = ""
    position = 0
    cash = 0.0
    last_valid_mid: float | None = None

    for row in prices.iter_rows(named=True):
        ts = int(row["timestamp"])
        od = _build_order_depth(row)

        state = TradingState(
            traderData=memory_str,
            timestamp=ts,
            listings={product: listing},
            order_depths={product: od},
            own_trades={product: []},
            market_trades={product: []},
            position={product: position},
            observations=empty_obs,
        )
        result, _conv, memory_str = trader.run(state)
        orders = list(result.get(product, []))

        # (b) take execution
        book_bids = dict(od.buy_orders)
        book_asks = dict(od.sell_orders)
        take_buy = take_sell = 0
        limit_orders: list[tuple[int, int]] = []

        for o in orders:
            px, qty = int(o.price), int(o.quantity)
            if qty > 0:
                remaining = qty
                for ask_px in sorted(book_asks.keys()):
                    if ask_px > px or remaining <= 0:
                        break
                    avail = -book_asks[ask_px]
                    fill = min(avail, remaining)
                    if fill > 0:
                        book_asks[ask_px] += fill
                        if book_asks[ask_px] == 0:
                            del book_asks[ask_px]
                        remaining -= fill
                        cash -= ask_px * fill
                        position += fill
                        take_buy += fill
                if remaining > 0:
                    limit_orders.append((px, remaining))
            elif qty < 0:
                remaining = -qty
                for bid_px in sorted(book_bids.keys(), reverse=True):
                    if bid_px < px or remaining <= 0:
                        break
                    avail = book_bids[bid_px]
                    fill = min(avail, remaining)
                    if fill > 0:
                        book_bids[bid_px] -= fill
                        if book_bids[bid_px] == 0:
                            del book_bids[bid_px]
                        remaining -= fill
                        cash += bid_px * fill
                        position -= fill
                        take_sell += fill
                if remaining > 0:
                    limit_orders.append((px, -remaining))

        # (d) bot-trade matching against resting limits
        limit_buy = limit_sell = 0
        pend_bids = sorted(
            [(px, q) for px, q in limit_orders if q > 0],
            key=lambda x: -x[0],
        )
        pend_asks = sorted(
            [(px, -q) for px, q in limit_orders if q < 0],
            key=lambda x: x[0],
        )
        for P, V in trades_map.get(ts, []):
            remaining_V = V
            # 严格更优才成交（对方优先同价规则）
            if pend_bids and P < pend_bids[0][0]:
                new_bids = []
                for px, q in pend_bids:
                    if P < px and remaining_V > 0:
                        fill = min(q, remaining_V)
                        cash -= px * fill
                        position += fill
                        limit_buy += fill
                        remaining_V -= fill
                        q -= fill
                    if q > 0:
                        new_bids.append((px, q))
                pend_bids = new_bids
            elif pend_asks and P > pend_asks[0][0]:
                new_asks = []
                for px, q in pend_asks:
                    if P > px and remaining_V > 0:
                        fill = min(q, remaining_V)
                        cash += px * fill
                        position -= fill
                        limit_sell += fill
                        remaining_V -= fill
                        q -= fill
                    if q > 0:
                        new_asks.append((px, q))
                pend_asks = new_asks

        mid = row.get("mid_price")
        mid_f = float(mid) if mid is not None else None
        if mid_f is not None and mid_f > 0:
            last_valid_mid = mid_f
        mark = last_valid_mid if last_valid_mid is not None else 0.0
        unrealized = position * mark
        pnl = cash + unrealized

        records.append({
            "timestamp": ts,
            "position": position,
            "cash": cash,
            "mid": mid_f,
            "pnl": pnl,
            "take_buy": take_buy,
            "take_sell": take_sell,
            "limit_buy": limit_buy,
            "limit_sell": limit_sell,
            "n_orders": len(orders),
        })

        if record_memory:
            try:
                memory_trace.append(json.loads(memory_str) if memory_str else {})
            except Exception:
                memory_trace.append({})

    results_df = pl.DataFrame(records)
    final_pnl = float(results_df["pnl"][-1])
    peak = float(results_df["pnl"].max())
    trough = float(results_df["pnl"].min())
    total_buy = int((results_df["take_buy"] + results_df["limit_buy"]).sum())
    total_sell = int((results_df["take_sell"] + results_df["limit_sell"]).sum())
    max_abs_pos = int(results_df["position"].abs().max())

    summary = {
        "final_pnl": final_pnl,
        "peak_pnl": peak,
        "trough_pnl": trough,
        "max_drawdown": peak - final_pnl if peak > final_pnl else 0.0,
        "max_abs_position": max_abs_pos,
        "total_buy_qty": total_buy,
        "total_sell_qty": total_sell,
        "n_ticks": results_df.shape[0],
    }
    return {"results": results_df, "summary": summary, "memory": memory_trace}


def simulate_multiday(trader, product: str, days: list[int] | None = None,
                      round_num: int = 1, record_memory: bool = False) -> dict:
    if days is None:
        days = [-2, -1, 0]

    all_prices_rows: list[dict] = []
    all_trades: list[tuple[int, float, int]] = []
    ts_offset = 0

    for day in days:
        prices = load_prices(day, product, round_num)
        trades = load_trades(day, product, round_num)

        for row in prices.iter_rows(named=True):
            r = dict(row)
            r["timestamp"] = int(r["timestamp"]) + ts_offset
            all_prices_rows.append(r)

        for row in trades.iter_rows(named=True):
            all_trades.append((
                int(row["timestamp"]) + ts_offset,
                float(row["price"]),
                int(row["quantity"]),
            ))

        max_ts = int(prices["timestamp"].max())
        ts_offset = max_ts + ts_offset + 100

    trades_map: dict[int, list[tuple[float, int]]] = {}
    for ts, price, qty in all_trades:
        trades_map.setdefault(ts, []).append((price, qty))

    listing = Listing(product, product, 1)
    empty_obs = Observation({}, {})

    records: list[dict] = []
    memory_trace: list[dict] = []
    memory_str = ""
    position = 0
    cash = 0.0
    last_valid_mid: float | None = None

    for row in all_prices_rows:
        ts = int(row["timestamp"])
        od = _build_order_depth(row)

        state = TradingState(
            traderData=memory_str,
            timestamp=ts,
            listings={product: listing},
            order_depths={product: od},
            own_trades={product: []},
            market_trades={product: []},
            position={product: position},
            observations=empty_obs,
        )
        result, _conv, memory_str = trader.run(state)
        orders = list(result.get(product, []))

        book_bids = dict(od.buy_orders)
        book_asks = dict(od.sell_orders)
        take_buy = take_sell = 0
        limit_orders: list[tuple[int, int]] = []

        for o in orders:
            px, qty = int(o.price), int(o.quantity)
            if qty > 0:
                remaining = qty
                for ask_px in sorted(book_asks.keys()):
                    if ask_px > px or remaining <= 0:
                        break
                    avail = -book_asks[ask_px]
                    fill = min(avail, remaining)
                    if fill > 0:
                        book_asks[ask_px] += fill
                        if book_asks[ask_px] == 0:
                            del book_asks[ask_px]
                        remaining -= fill
                        cash -= ask_px * fill
                        position += fill
                        take_buy += fill
                if remaining > 0:
                    limit_orders.append((px, remaining))
            elif qty < 0:
                remaining = -qty
                for bid_px in sorted(book_bids.keys(), reverse=True):
                    if bid_px < px or remaining <= 0:
                        break
                    avail = book_bids[bid_px]
                    fill = min(avail, remaining)
                    if fill > 0:
                        book_bids[bid_px] -= fill
                        if book_bids[bid_px] == 0:
                            del book_bids[bid_px]
                        remaining -= fill
                        cash += bid_px * fill
                        position -= fill
                        take_sell += fill
                if remaining > 0:
                    limit_orders.append((px, -remaining))

        limit_buy = limit_sell = 0
        pend_bids = sorted(
            [(px, q) for px, q in limit_orders if q > 0],
            key=lambda x: -x[0],
        )
        pend_asks = sorted(
            [(px, -q) for px, q in limit_orders if q < 0],
            key=lambda x: x[0],
        )
        for P, V in trades_map.get(ts, []):
            remaining_V = V
            if pend_bids and P < pend_bids[0][0]:
                new_bids = []
                for px, q in pend_bids:
                    if P < px and remaining_V > 0:
                        fill = min(q, remaining_V)
                        cash -= px * fill
                        position += fill
                        limit_buy += fill
                        remaining_V -= fill
                        q -= fill
                    if q > 0:
                        new_bids.append((px, q))
                pend_bids = new_bids
            elif pend_asks and P > pend_asks[0][0]:
                new_asks = []
                for px, q in pend_asks:
                    if P > px and remaining_V > 0:
                        fill = min(q, remaining_V)
                        cash += px * fill
                        position -= fill
                        limit_sell += fill
                        remaining_V -= fill
                        q -= fill
                    if q > 0:
                        new_asks.append((px, q))
                pend_asks = new_asks

        mid = row.get("mid_price")
        mid_f = float(mid) if mid is not None else None
        if mid_f is not None and mid_f > 0:
            last_valid_mid = mid_f
        mark = last_valid_mid if last_valid_mid is not None else 0.0
        pnl = cash + position * mark

        records.append({
            "timestamp": ts,
            "position": position,
            "cash": cash,
            "mid": mid_f,
            "pnl": pnl,
            "take_buy": take_buy,
            "take_sell": take_sell,
            "limit_buy": limit_buy,
            "limit_sell": limit_sell,
            "n_orders": len(orders),
        })

        if record_memory:
            try:
                memory_trace.append(json.loads(memory_str) if memory_str else {})
            except Exception:
                memory_trace.append({})

    results_df = pl.DataFrame(records)
    final_pnl = float(results_df["pnl"][-1])
    peak = float(results_df["pnl"].max())
    trough = float(results_df["pnl"].min())
    total_buy = int((results_df["take_buy"] + results_df["limit_buy"]).sum())
    total_sell = int((results_df["take_sell"] + results_df["limit_sell"]).sum())
    max_abs_pos = int(results_df["position"].abs().max())

    per_day_pnl: list[float] = []
    ticks_per_day = len(all_prices_rows) // len(days)
    for i in range(len(days)):
        end_idx = min((i + 1) * ticks_per_day, len(records)) - 1
        per_day_pnl.append(float(records[end_idx]["pnl"]) -
                           (float(records[i * ticks_per_day - 1]["pnl"]) if i > 0 else 0.0))

    summary = {
        "final_pnl": final_pnl,
        "peak_pnl": peak,
        "trough_pnl": trough,
        "max_drawdown": peak - final_pnl if peak > final_pnl else 0.0,
        "max_abs_position": max_abs_pos,
        "total_buy_qty": total_buy,
        "total_sell_qty": total_sell,
        "n_ticks": results_df.shape[0],
        "per_day_pnl": per_day_pnl,
        "days": days,
    }
    return {"results": results_df, "summary": summary, "memory": memory_trace}


# --- Plotting -----------------------------------------------------------------

def plot_results(sim_result: dict, show: bool = True, savepath: str | None = None):
    import matplotlib.pyplot as plt

    df = sim_result["results"]
    fig, axes = plt.subplots(4, 1, figsize=(14, 11), sharex=True)

    ts = df["timestamp"].to_numpy()
    import numpy as np

    axes[0].plot(ts, df["pnl"].to_numpy(), lw=1.0, color="steelblue", label="Total PnL")
    axes[0].axhline(0, color="black", lw=0.4)
    axes[0].set_ylabel("PnL")
    axes[0].legend(loc="upper left")
    axes[0].grid(alpha=0.3)

    axes[1].plot(ts, df["position"].to_numpy(), lw=0.8, color="purple")
    axes[1].axhline(80, color="red", ls="--", alpha=0.3)
    axes[1].axhline(-80, color="red", ls="--", alpha=0.3)
    axes[1].axhline(0, color="black", lw=0.4)
    axes[1].set_ylabel("Position")
    axes[1].grid(alpha=0.3)

    mid_arr = df["mid"].to_numpy().astype(float)
    mid_arr = np.where((mid_arr <= 0) | np.isnan(mid_arr), np.nan, mid_arr)
    axes[2].plot(ts, mid_arr, lw=0.7, alpha=0.7, label="Mid")
    if sim_result.get("memory"):
        inner = [m.get("last_inner_fair") for m in sim_result["memory"]]
        xs = [t for t, f in zip(ts, inner) if f is not None]
        ys = [f for f in inner if f is not None]
        if ys:
            axes[2].plot(xs, ys, lw=0.6, alpha=0.8, label="inner_fair", color="orange")
    axes[2].set_ylabel("Price")
    axes[2].legend(loc="upper left")
    axes[2].grid(alpha=0.3)

    buy_qty = (df["take_buy"] + df["limit_buy"]).to_numpy()
    sell_qty = (df["take_sell"] + df["limit_sell"]).to_numpy()
    axes[3].bar(ts, buy_qty, width=80, color="green", alpha=0.6, label="Buy")
    axes[3].bar(ts, -sell_qty, width=80, color="red", alpha=0.6, label="Sell")
    axes[3].axhline(0, color="black", lw=0.4)
    axes[3].set_ylabel("Fills")
    axes[3].legend(loc="upper left")
    axes[3].grid(alpha=0.3)

    axes[-1].set_xlabel("Timestamp")
    plt.tight_layout()
    if savepath:
        plt.savefig(savepath, dpi=120)
    if show:
        plt.show()
    return fig


# --- Convenience strategy loader ---------------------------------------------

def load_trader_from(path: str | Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("strat", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Trader()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=str(Path(__file__).parent / "策略v2.py"))
    ap.add_argument("--product", default="ASH_COATED_OSMIUM")
    ap.add_argument("--day", type=int, default=0)
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    trader = load_trader_from(args.strategy)
    result = simulate(trader, product=args.product, day=args.day,
                      round_num=args.round, record_memory=args.plot)
    print(json.dumps(result["summary"], indent=2))
    if args.plot:
        plot_results(result, show=args.save is None, savepath=args.save)
