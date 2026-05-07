"""Prosperity 4 backtest wrapper around prosperity4bt.

接受与 prosperity4bt CLI 完全相同的 flag。调用前：
  - 向 prosperity4bt.data.LIMITS 注入 P4 产品的持仓上限
  - 把当前工作目录加入 sys.path 使 `from datamodel import ...` 能走 prosperity4bt
  - 跨天 carryover: state.position / traderData / data.profit_loss（否则每天都 reset
    会导致 review_plot 累计仓位越过上限，并且让 --merge-pnl 的 offset 与真实 MTM 脱节）
  - 空订单簿 (mid_price=0) 时用上一次有效 mid 做 mark-to-market，避免 PnL 插针

用法:
    python backtest/run.py final.py 1-0 --data ./data/bt
    python backtest/run.py final.py 1   --data ./data/bt
    python backtest/run.py final.py 1-0 --data ./data/bt --out backtest/runs/mytest.log
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prosperity4bt import __main__ as _main
from prosperity4bt import data as _bt_data
from prosperity4bt import runner as _runner
from prosperity4bt.data import read_day_data
from prosperity4bt.datamodel import Observation, Trade, TradingState
from prosperity4bt.models import ActivityLogRow, BacktestResult, SandboxLogRow

try:
    from IPython.utils.io import Tee
except Exception:  # noqa: BLE001
    Tee = None
from tqdm import tqdm

# P4 产品持仓上限 — 新产品在这里加一行即可
_bt_data.LIMITS.update({
    "ASH_COATED_OSMIUM": 80,
    "INTARIAN_PEPPER_ROOT": 80,
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300,
    "VEV_4500": 300,
    "VEV_5000": 300,
    "VEV_5100": 300,
    "VEV_5200": 300,
    "VEV_5300": 300,
    "VEV_5400": 300,
    "VEV_5500": 300,
    "VEV_6000": 300,
    "VEV_6500": 300,
    # round 5 (POS_LIMIT=10 for all)
    "PEBBLES_XS": 10, "PEBBLES_S": 10, "PEBBLES_M": 10,
    "PEBBLES_L": 10, "PEBBLES_XL": 10,
    "GALAXY_SOUNDS_DARK_MATTER": 10, "GALAXY_SOUNDS_BLACK_HOLES": 10,
    "GALAXY_SOUNDS_PLANETARY_RINGS": 10, "GALAXY_SOUNDS_SOLAR_WINDS": 10,
    "GALAXY_SOUNDS_SOLAR_FLAMES": 10,
    "SLEEP_POD_SUEDE": 10, "SLEEP_POD_LAMB_WOOL": 10, "SLEEP_POD_POLYESTER": 10,
    "SLEEP_POD_NYLON": 10, "SLEEP_POD_COTTON": 10,
    "MICROCHIP_CIRCLE": 10, "MICROCHIP_OVAL": 10, "MICROCHIP_SQUARE": 10,
    "MICROCHIP_RECTANGLE": 10, "MICROCHIP_TRIANGLE": 10,
    "ROBOT_VACUUMING": 10, "ROBOT_MOPPING": 10, "ROBOT_DISHES": 10,
    "ROBOT_LAUNDRY": 10, "ROBOT_IRONING": 10,
    "UV_VISOR_YELLOW": 10, "UV_VISOR_AMBER": 10, "UV_VISOR_ORANGE": 10,
    "UV_VISOR_RED": 10, "UV_VISOR_MAGENTA": 10,
    "TRANSLATOR_SPACE_GRAY": 10, "TRANSLATOR_ASTRO_BLACK": 10,
    "TRANSLATOR_ECLIPSE_CHARCOAL": 10, "TRANSLATOR_GRAPHITE_MIST": 10,
    "TRANSLATOR_VOID_BLUE": 10,
    "PANEL_1X2": 10, "PANEL_2X2": 10, "PANEL_1X4": 10, "PANEL_2X4": 10,
    "PANEL_4X4": 10,
    "OXYGEN_SHAKE_MORNING_BREATH": 10, "OXYGEN_SHAKE_EVENING_BREATH": 10,
    "OXYGEN_SHAKE_MINT": 10, "OXYGEN_SHAKE_CHOCOLATE": 10,
    "OXYGEN_SHAKE_GARLIC": 10,
    "SNACKPACK_CHOCOLATE": 10, "SNACKPACK_VANILLA": 10,
    "SNACKPACK_PISTACHIO": 10, "SNACKPACK_STRAWBERRY": 10,
    "SNACKPACK_RASPBERRY": 10,
})


# ── 跨天 carryover：run_backtest 每次被调用会 reset state，这里把结束状态暂存起来 ──
_CARRYOVER: dict = {"position": {}, "trader_data": "", "profit_loss": {}}

# 跨天 timestamp offset：每天数据的 prices/trades/observations key 原本都从 0 起，
# trader 里常用 state.timestamp 做绝对时间 (e.g. fair = MU * t + b)，日界 reset 会让
# trader 逻辑错乱（day 0 的 t=0 对应的 fair 仍按 day-1 末端学到的 b 展开 → 报价离 mid
# 1000 点，整日打不成交易）。改为累加：day-1 用 [0, 999900]，day0 用 [1000000, 1999900]…
_TS_OFFSET_STATE: dict = {"next_offset": 0}
_DAY_TS_SPAN = 1_000_000  # 单日 timestamp 跨度（0..999900，间隔 100，合计 1e6）

# 上一个有效 mid（用于 mid=0 时的 MTM 兜底）
_LAST_MID: dict[str, float] = {}


def _patched_create_activity_logs(state, data, result) -> None:
    """拷贝自 prosperity4bt.runner.create_activity_logs，但当 row.mid_price==0
    （空订单簿 / 原始 CSV 无报价）时改用上一次有效 mid 做 mark-to-market，
    避免 position * 0 造成的 PnL 插针。"""
    for product in data.products:
        row = data.prices[state.timestamp][product]

        raw_mid = row.mid_price
        if raw_mid and raw_mid > 0:
            _LAST_MID[product] = raw_mid
            mid_for_pnl = raw_mid
            mid_for_log = raw_mid
        else:
            fallback = _LAST_MID.get(product)
            mid_for_pnl = fallback if fallback is not None else 0.0
            mid_for_log = fallback if fallback is not None else raw_mid

        product_profit_loss = data.profit_loss[product]
        position = state.position.get(product, 0)
        if position != 0 and mid_for_pnl > 0:
            product_profit_loss += position * mid_for_pnl

        bid_prices_len = len(row.bid_prices)
        bid_volumes_len = len(row.bid_volumes)
        ask_prices_len = len(row.ask_prices)
        ask_volumes_len = len(row.ask_volumes)

        columns = [
            result.day_num,
            state.timestamp,
            product,
            row.bid_prices[0] if bid_prices_len > 0 else "",
            row.bid_volumes[0] if bid_volumes_len > 0 else "",
            row.bid_prices[1] if bid_prices_len > 1 else "",
            row.bid_volumes[1] if bid_volumes_len > 1 else "",
            row.bid_prices[2] if bid_prices_len > 2 else "",
            row.bid_volumes[2] if bid_volumes_len > 2 else "",
            row.ask_prices[0] if ask_prices_len > 0 else "",
            row.ask_volumes[0] if ask_volumes_len > 0 else "",
            row.ask_prices[1] if ask_prices_len > 1 else "",
            row.ask_volumes[1] if ask_volumes_len > 1 else "",
            row.ask_prices[2] if ask_prices_len > 2 else "",
            row.ask_volumes[2] if ask_volumes_len > 2 else "",
            mid_for_log,
            product_profit_loss,
        ]

        result.activity_logs.append(ActivityLogRow(columns))


def _patched_run_backtest(
    trader,
    file_reader,
    round_num,
    day_num,
    print_output,
    trade_matching_mode,
    no_names,
    show_progress_bar,
) -> BacktestResult:
    """与官方 run_backtest 一致，但：
    - state.position / traderData / data.profit_loss 来自 `_CARRYOVER`
      (使跨天仓位上限真正生效，且让 MTM 在日界保持连续)
    - 日结束时把最终 position / traderData / profit_loss 存回 `_CARRYOVER`
    """
    data = read_day_data(file_reader, round_num, day_num, no_names)

    for product, value in _CARRYOVER["profit_loss"].items():
        if product in data.profit_loss:
            data.profit_loss[product] = value

    # 把 prices/trades/observations 的 timestamp 统一加上跨天 offset，保证
    # state.timestamp 对 trader 来说是一条连续的时间轴
    ts_offset = _TS_OFFSET_STATE["next_offset"]
    if ts_offset:
        data.prices = {k + ts_offset: v for k, v in data.prices.items()}
        # 必须是 defaultdict(defaultdict(list))，因为 runner.match_orders 对无成交
        # 的 tick 直接做 data.trades[ts].items() —— 普通 dict 会 KeyError
        shifted_trades: defaultdict = defaultdict(lambda: defaultdict(list))
        for k, by_sym in data.trades.items():
            for sym, tlist in by_sym.items():
                shifted_trades[k + ts_offset][sym] = [
                    Trade(t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp + ts_offset)
                    for t in tlist
                ]
        data.trades = shifted_trades
        data.observations = {k + ts_offset: v for k, v in data.observations.items()}

    os.environ["PROSPERITY4BT_ROUND"] = str(round_num)
    os.environ["PROSPERITY4BT_DAY"] = str(day_num)

    trader_data = _CARRYOVER["trader_data"]
    state = TradingState(
        traderData=trader_data,
        timestamp=0,
        listings={},
        order_depths={},
        own_trades={},
        market_trades={},
        position=dict(_CARRYOVER["position"]),
        observations=Observation({}, {}),
    )

    result = BacktestResult(
        round_num=data.round_num,
        day_num=data.day_num,
        sandbox_logs=[],
        activity_logs=[],
        trades=[],
    )

    timestamps = sorted(data.prices.keys())
    timestamps_iterator = tqdm(timestamps, ascii=True) if show_progress_bar else timestamps

    for timestamp in timestamps_iterator:
        state.timestamp = timestamp
        state.traderData = trader_data

        _runner.prepare_state(state, data)

        stdout = StringIO()
        stdout.close = lambda: None  # type: ignore[method-assign]

        if print_output and Tee is not None:
            with closing(Tee(stdout)):
                orders, conversions, trader_data = trader.run(state)
        else:
            with redirect_stdout(stdout):
                orders, conversions, trader_data = trader.run(state)

        sandbox_row = SandboxLogRow(
            timestamp=timestamp,
            sandbox_log="",
            lambda_log=stdout.getvalue().rstrip(),
        )
        result.sandbox_logs.append(sandbox_row)

        _runner.type_check_orders(orders)
        _runner.create_activity_logs(state, data, result)
        _runner.enforce_limits(state, data, orders, sandbox_row)
        _runner.match_orders(state, data, orders, result, trade_matching_mode)

    _CARRYOVER["position"] = dict(state.position)
    _CARRYOVER["trader_data"] = trader_data
    _CARRYOVER["profit_loss"] = dict(data.profit_loss)
    _TS_OFFSET_STATE["next_offset"] = ts_offset + _DAY_TS_SPAN
    return result


_original_merge_results = _main.merge_results


def _patched_merge_results(a, b, merge_profit_loss, merge_timestamps):
    """carryover 后两件事都已在 run 内部处理：
    - profit_loss 在日界连续 → 不能再叠 merge_pnl offset
    - timestamp 已在读 data 时统一加 offset → 不能再叠 merge_timestamps offset
    所以都强制走 False，merge_results 退化为单纯的 log 拼接。"""
    return _original_merge_results(a, b, False, False)


_runner.create_activity_logs = _patched_create_activity_logs
_runner.run_backtest = _patched_run_backtest
_main.run_backtest = _patched_run_backtest
_main.merge_results = _patched_merge_results


from prosperity4bt.__main__ import app  # noqa: E402


if __name__ == "__main__":
    app()
