# IMC Prosperity Workspace

本地研究 / 回测 / 提交的统一工作目录。Round 1 产品：**EMERALDS（翡翠）** 和 **TOMATOES（番茄）**。

## 目录结构

```
imc/
├── prosperity/                    # 所有代码和数据都在这里
│   ├── trader.py                  # ★ 提交给 IMC 的策略入口，唯一会上传的文件
│   ├── datamodel.py               # 本地 shim（不要上传），只是重导出 prosperity3bt.datamodel
│   ├── configs/
│   │   └── default.yaml           # per-product 参数（fair、offset、pos 阈值）
│   ├── data/
│   │   ├── raw/                   # 官方下发的 prices_* / trades_* CSV
│   │   ├── processed/             # 研究过程中生成的中间数据
│   │   └── submissions/           # 历史提交的原始 log
│   ├── backtest/
│   │   └── runner.py              # 薄包装：subprocess 调 prosperity3bt
│   ├── utils/
│   │   ├── dataio.py              # load_prices / load_prices_wide / load_trades
│   │   ├── orderbook.py           # wall_mid, spread, ACF, adverse selection, 间隔分布
│   │   └── viz.py                 # plotly 图：orderbook scatter / normalized / ACF / ...
│   ├── 1_microstructure.ipynb     # 微结构研究（价差、ACF、adverse selection）
│   ├── 2_strategy_dev.ipynb       # 策略原型 + 向量化 taker PnL
│   └── 3_review.ipynb             # 提交回来的 log 复盘
├── TUTORIAL_ROUND_1/              # 官方 tutorial 阶段原始数据备份
├── backtests/                     # prosperity3bt 每次运行产出的 log（追加式）
├── new_strategy.md                # 当前 round 的策略设计文档
├── engineering_drawing.md
└── microstructure_research_playbook.md
```

## 环境

项目强约束：所有命令都在这个 venv 下跑。

```bash
source ~/baseChain/defi/bin/activate
```

首次拉项目之后需要装一次回测器：

```bash
pip install prosperity4bt
```

> 注意：Prosperity 4 的社区回测器叫 `prosperity4bt`（PyPI 版本号写的是 0.0.0，但内容是完整的）。不要装 `prosperity3bt`——那是上一届的，产品名和数据完全对不上。

## 研究流程

1. 把当天的官方 CSV 丢进 [prosperity/data/raw/](prosperity/data/raw/)，命名格式 `prices_round_<R>_day_<D>.csv` / `trades_round_<R>_day_<D>.csv`，`;` 分隔，列名跟 IMC 官方一致。
2. 打开 [prosperity/1_microstructure.ipynb](prosperity/1_microstructure.ipynb) 改头几格的 `ROUND` / `DAY` / `PRODUCT`，一路往下跑：
   - orderbook 散点图（`plot_orderbook_scatter`）—— 看 wall、看成交落点；
   - normalized 图（`plot_normalized_orderbook`）—— 价格减 wall_mid 去趋势；
   - spread / wall_mid 统计；
   - return ACF —— 决定"可预测 vs 做市"；
   - adverse selection —— 决定 taker 是 informed 还是噪声；
   - interval 分布 —— 订单 / 成交节奏。
3. 策略原型去 [prosperity/2_strategy_dev.ipynb](prosperity/2_strategy_dev.ipynb)。里面有一个 `vectorized_taker_pnl`，用 numpy 直接算 "每 tick 吃掉所有越过 fair 的单" 的上界 PnL，远快于 full backtest，用来筛参数。
4. 参数确定后写进 [prosperity/trader.py](prosperity/trader.py) 和 [prosperity/configs/default.yaml](prosperity/configs/default.yaml)。
5. 跑本地回测（见下一节）。
6. 提交到 IMC，拿到 log 丢 [prosperity/data/submissions/](prosperity/data/submissions/)，在 [prosperity/3_review.ipynb](prosperity/3_review.ipynb) 里复盘。

## 本地回测（prosperity4bt）

### 最小可运行例子

```bash
source ~/baseChain/defi/bin/activate
cd /Users/leoliu/imc
prosperity4bt prosperity/trader.py 0
```

就这一条。`0` 代表 round 0（tutorial），回测器会自动跑它内置的 day -2 和 day -1 两天 EMERALDS/TOMATOES 数据，最后打印每天 PnL 和总 PnL，同时在 [backtests/](backtests/) 写一份 log。

### 常用变体

```bash
# 单天
prosperity4bt prosperity/trader.py 0--1        # round 0 day -1（中间两个减号）

# 边跑边把 trader 里的 print() 输出到终端（debug 策略时最好用）
prosperity4bt prosperity/trader.py 0 --print

# 保守撮合：只有当历史成交价严格劣于你的报价时才算你成交（压一压回测乐观偏差）
prosperity4bt prosperity/trader.py 0 --match-trades worse

# 跑完自动把 log 上传到 jmerle 的在线可视化页
prosperity4bt prosperity/trader.py 0 --vis

# 用本地 prosperity/data/raw 下的 CSV 覆盖包自带的历史数据（等 IMC 发真实 round 1 行情后用）
prosperity4bt prosperity/trader.py 1 --data prosperity

# 不保存 log 到 backtests/
prosperity4bt prosperity/trader.py 0 --no-out
```

每次运行（除非带 `--no-out`）会在 [backtests/](backtests/) 里追加一份 `YYYY-MM-DD_HH-MM-SS.log`，内容包含每一 tick 的 orderbook、sandbox log、你发出的订单、成交记录。可以丢进 3_review.ipynb 或者 jmerle 的可视化页去看。

### 重要：tutorial round 0 的限额跟真实 round 不一样

prosperity4bt 目前只自带 **round 0 tutorial** 的行情数据。tutorial 的 sandbox 对 TOMATOES 的硬限额是 **80**，而真实 round 1 是 **200**。

所以在 tutorial 数据上跑你的 trader 时，只要 TOMATOES 挂单量 > 80，就会被回测器整批拒掉，表现为 `TOMATOES: 0`（零成交）。**这不是你代码的 bug，是 tutorial 的人为限制**。等 IMC 发了真实 round 1 数据，把 CSV 丢进 `prosperity/data/raw/` 并加 `--data prosperity` 就可以了。

**千万不要因为这个把 [trader.py:21](prosperity/trader.py#L21) 的 `LIMITS["TOMATOES"]` 从 200 改成 80**——那是提交到 IMC 的版本，改了在真实 round 1 就浪费了 120 手的额度。

也可以从 Python 里调用 [prosperity/backtest/runner.py](prosperity/backtest/runner.py)：

```python
from prosperity.backtest.runner import run_backtest, run_all_days
run_backtest(round_num=1, day=0)
run_all_days(round_num=1, days=[-2, -1, 0])
```

> `runner.py` 目前的 `--round / --day` CLI 写法是老版 prosperity3bt 的参数。新版已经改成位置参数 `1-0` 的格式，这个文件要同步更新，暂时建议直接用上面的命令行。

### 关于 `datamodel.py` shim

[prosperity/datamodel.py](prosperity/datamodel.py) 只有两行：

```python
from prosperity4bt.datamodel import *
```

原因：prosperity4bt 用 `importlib.import_module` 加载 trader，`from datamodel import Order, TradingState` 在本地找不到包会直接报 `No module named 'datamodel'`。这个 shim 让本地 import 走到 prosperity4bt 打包的那份 datamodel。

**重要**：这个文件**不要上传**到 IMC。服务器会自己注入 `datamodel`，多传一份会冲突。提交只传 [prosperity/trader.py](prosperity/trader.py) 这**一个**文件。

## 提交给 IMC

- 只提交 [prosperity/trader.py](prosperity/trader.py) 这一个文件。
- `Trader.run(state)` 的返回签名必须是 `(orders: dict[Symbol, list[Order]], conversions: int, traderData: str)`。
- `Order(symbol, price, quantity)`：`price` 必须是 int，`quantity` 正数是买、负数是卖。
- 每一笔 `Order` 的 `symbol` 必须来自 `state.order_depths.keys()`——**不要硬编码产品名**，用 `for product in state.order_depths:` 遍历，或者先打印一次 key 确认拼写。
- Position limit 由服务器硬拦，超限的那一侧整批订单会被整体拒绝，务必在本地就算好 `buy_cap` / `sell_cap`。
- `traderData` 是 tick 之间的状态桶，字符串类型（通常 `json.dumps`），本 tick 返回的值会在下个 tick 通过 `state.traderData` 读回。

## 回测 vs 真实服务器的差异（已知）

| 维度 | prosperity4bt 本地 | IMC 真实服务器 |
|------|--------------------|----------------|
| 对手方 | 从历史 market trades 回放模拟 | 活 bot，会对你的挂单做反应 |
| 同价排队 | 通常算你成交 | 你排在 bot 后面，可能拿不到量 |
| 延迟 / tick 内排序 | 无 | 有 |
| 数据 | 目前只有 round 0 tutorial | 当前 round 的真实行情 |
| Position limit | tutorial 限额（EMERALDS 20 / TOMATOES **80**） | 真实 round 限额（EMERALDS 20 / TOMATOES **200**） |

结论：本地 PnL 对 penny-the-wall 类策略**系统性偏乐观**。建议双跑 `--match-trades all` 和 `--match-trades worse`，真实 PnL 大概率在两者之间。

## 已知待办

- [prosperity/trader.py:209-214](prosperity/trader.py#L209-L214) `_wall_mid` 的 ask 侧取的是**最小** abs 卖量，应该是**最大**。会让 fair 漂到散单上、quote 明显偏位。
- [prosperity/backtest/runner.py](prosperity/backtest/runner.py) 的 CLI 参数和可执行文件名都是旧的 `prosperity3bt ... --round / --day`，需要换成 `prosperity4bt` + 位置参数格式（`0` / `0--1`）。
- [prosperity/data/submissions/](prosperity/data/submissions/) 目前还是空的，每次 IMC 提交完记得把官方 log 存进来，方便 3_review 复盘。
