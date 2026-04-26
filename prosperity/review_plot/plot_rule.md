# `3_review.ipynb` 绘图部件迁移规范（plot_rule）

本文件定义如何把 [3_review.ipynb](3_review.ipynb) 中散落的图形逻辑抽离成一个结构
清晰、职责分离、可在任意提交 log 上复用的绘图部件。目标：让研究 notebook
仅保留函数调用和功能开关，其它所有逻辑都落到代码文件里。

---

## 0. 文件夹结构

所有代码都放在 `prosperity/review_plot/`。结构刻意保持扁平，每个文件只做一件事：

```
prosperity/
├── 3_review.ipynb                    # 研究 notebook（仅调用 + 开关）
└── review_plot/
    ├── __init__.py                   # 对外导出 load_submission / ReviewContext / 绘图函数
    ├── dataio.py                     # log / json → polars（ob_wide、ob_long、trades、pnl）
    ├── context.py                    # ReviewContext：统一持有 ob/trades/fair/position/pnl
    ├── enrich.py                     # 计算 position_df、pnl_by_product、my_with_fair(edge/edge_pnl)
    ├── filters.py                    # 所有按量 / 按 edge / 按时间的过滤器
    ├── resample.py                   # Plotly resample 劫持（独立可开关）
    ├── markers.py                    # 单一职责：把一组点做成一个 go.Scatter trace
    ├── plots/
    │   ├── main_review.py            # 三行主图：Orderbook + Position + PnL
    │   ├── normalized_review.py      # 三行去漂移图：Δ from Fair + Position + PnL
    │   ├── edge_scatter.py           # Step 5b：Edge per trade 散点
    │   ├── fill_histogram.py         # Step 5c：成交档位直方图
    │   ├── trade_interval.py         # Step 6：成交间隔直方图
    │   ├── pnl_attribution.py        # Step 7：edge 累计 vs 总 PnL
    │   └── summary_table.py          # Step 8：汇总统计表（返回 DataFrame）
    └── fair/
        ├── __init__.py               # get_fair(product, ob_wide) 分派到下面的产品文件
        ├── base.py                   # 通用 wall_mid / 兜底 fair
        ├── ASH_COATED_OSMIUM.py      # 产品一的 fair（max-vol>20 两侧均值 + forward fill）
        ├── INTARIAN_PEPPER_ROOT.py   # 产品二的 fair（int(13000 + 0.001 * t)）
        └── <NEW_PRODUCT>.py          # 新增产品只需加一个文件 + 注册到 __init__
```

研究 notebook 的 cell 数量严格控制在 ≤ 8 个，每个 cell 几行：

```python
# cell 1 —— 只设开关
from review_plot import load_submission, ReviewContext
from review_plot.plots import (
    plot_main_review, plot_normalized_review, plot_edge_scatter,
    plot_fill_histogram, plot_trade_interval, plot_pnl_attribution, build_summary,
)
from review_plot.resample import enable_plotly_resample

SUBMISSION_ID = "result"
enable_plotly_resample(max_points=4000)      # 降采样开关

# cell 2 —— 载入 + 构造 context
ctx = ReviewContext.from_submission(
    submission_id=SUBMISSION_ID,
    my_trades_dir="data/my_trades",
    qty_filter={1, 2, 5, 10, 20},            # 按成交量白名单
    cross_fair_only=False,                   # 非理性挂单开关
    near_fair_tick=4,                        # |Δ|<=4 的挂单高亮
)

# cell 3 —— 主图
plot_main_review(ctx, "INTARIAN_PEPPER_ROOT").show()
plot_main_review(ctx, "ASH_COATED_OSMIUM").show()

# cell 4 —— normalized
plot_normalized_review(ctx, "ASH_COATED_OSMIUM").show()

# cell 5 ~ 7 —— edge / fill histogram / interval / attribution
plot_edge_scatter(ctx).show()
plot_fill_histogram(ctx).show()
plot_trade_interval(ctx).show()
plot_pnl_attribution(ctx).show()

# cell 8 —— summary
build_summary(ctx)
```

---

## 1. 绘制主图所需的具体交易信息

所有信息都在 `ReviewContext` 中以 polars DataFrame 形式给出，由 `dataio.py`
解析 `.log` + `.json` 并由 `enrich.py` 扩展。下列字段是**绘图所需的最小完备集**。

| DataFrame | 关键列 | 用途 |
|---|---|---|
| `ob_wide` | `timestamp, day, product, mid_price, bid_price_{1..3}, bid_volume_{1..3}, ask_price_{1..3}, ask_volume_{1..3}, profit_and_loss` | Fair 计算源头、分产品 PnL 曲线 |
| `ob_long` | `timestamp, product, price, volume, side∈{bid,ask}, level∈{1,2,3}` | 散点图（三档） |
| `fair_df` | `timestamp, product, fair` | 所有 Δ-from-fair 计算 |
| `all_trades` | `timestamp, product, price, quantity, buyer, seller, trade_type∈{my_buy,my_sell,market}` | 区分我方 / 市场成交 |
| `my_trades` | `all_trades` 里 `trade_type != market` 的子集 | 我方标记、edge 计算 |
| `mkt_trades` | `all_trades` 里 `trade_type == market` 的子集 | 市场成交标记 |
| `my_with_fair` | `my_trades + fair + edge + edge_pnl + delta(= price - fair)` | Normalized 主图、edge 分析、PnL 归因 |
| `position_df` | `timestamp, product, position`（含 `t=0` 初始 0） | 仓位子图（阶梯线） |
| `pnl_by_product` | `timestamp, product, profit_and_loss` | PnL 子图 |
| `pnl_total` | `json_data["graphLog"]` 解析结果 | 总 PnL 曲线（可选） |

> 约定：`edge = (fair - price) if my_buy else (price - fair)`，`edge_pnl = edge * quantity`，`delta = price - fair`。
> 所有 DataFrame 都按 `(product, timestamp)` 排序后再传入绘图层，绘图层不负责排序。

---

## 2. 标记清单（Markers）

所有订单 / 成交类的 marker 都**必须**在 `customdata` 中携带三个基础字段，并体现在
hover 模板上：`qty`（挂单量或成交量）、`px`（绝对价格）、`delta`（price − fair）。
`markers.py` 中提供统一的 `build_marker_trace(df, spec)` 函数，`spec` 规定 marker
形状 / 颜色 / y 轴用什么列。

| Marker | Row | 形状 / 颜色 | 必带字段（customdata） | 备注 |
|---|---|---|---|---|
| **Bids** | Row1 | `circle` / `steelblue`，size ∝ volume / max_vol | `qty, px, delta` | 主图 y=px，normalized 图 y=delta |
| **Asks** | Row1 | `circle` / `salmon`，size ∝ volume / max_vol | `qty, px, delta` | 同上 |
| **Cross-Fair Orders** (非理性挂单：bid>fair 或 ask<fair) | Row1 | `x` / `gold`，黑描边，size ∝ volume（下限 10，上限 28） | `qty, px, delta, side, level` | hover 强调 px + qty |
| **Near-Fair Rational Orders** (\|delta\|≤`near_fair_tick` 且 bid≤fair / ask≥fair) | Row1 | `diamond` / `purple`，indigo 描边，opacity=0.9，size ∝ volume（下限 8，上限 20） | `qty, px, delta, side, level` | 可调阈值见开关 |
| **Market Trades** | Row1 | `x` / `black`，size=7 | `qty, px, delta` | |
| **My Buy** | Row1 | `triangle-up` / `limegreen`，darkgreen 描边，size=10 | `qty, px, delta, edge, edge_pnl` | edge 参与 hover 展示 |
| **My Sell** | Row1 | `triangle-down` / `red`，darkred 描边，size=10 | `qty, px, delta, edge, edge_pnl` | 同上 |
| **Fair Line** | Row1（主图） | line / `green` 1.5px | —— | 只画绝对图；normalized 图用 `y=0` 的 `add_hline` |
| **Fair Trend (右副轴)** | Row1（normalized） | line / `green` dotted 1.2px，`yaxis='y4'` | —— | 只出现在 normalized 图上 |
| **Position** | Row2 | hv-step line / `purple` 1.5px | `qty=None`，hover: `t, pos` | 数据来自 `position_df` |
| **Zero Line** | Row2 & Row3 | dashed gray 0.5px | —— | `add_hline(y=0)` |
| **PnL (per product)** | Row3 | line / `orange` 1.5px | hover: `t, pnl` | 数据来自 `pnl_by_product` |
| **Edge Scatter** | Step 5b | `circle`，正绿 / 零灰 / 负红，size=8，黑描边 0.5 | `qty, px, delta, trade_type, edge` | y 轴 = edge |
| **Fill Histogram (buy / sell)** | Step 5c | `histogram`，buy=`limegreen`，sell=`red`，opacity=0.7，barmode=overlay | `price - fair`（即 delta） | 垂直线 x=0 |
| **Interval Histogram** | Step 6 | `histogram`，`steelblue`，nbinsx=40 | `Δtimestamp`（ms） | |
| **Cum Edge PnL** | Step 7 | line+markers / `green` 1.5px | `timestamp, cum_edge_pnl` | |
| **Total PnL** | Step 7 | line / `orange` 1.5px | `timestamp, profit_and_loss` | |

**hover 模板统一模板**（由 `markers.py` 生成，不要在外部复制粘贴）：

```
"t=%{x}<br>y=%{y}<br>qty=%{customdata[0]}<br>px=%{customdata[1]}<br>Δ=%{customdata[2]}"
```

如果该 trace 另有 `edge` / `edge_pnl` 字段，追加 `<br>edge=%{customdata[3]}<br>edge_pnl=%{customdata[4]}`。

---

## 3. Fair Price 模块（`review_plot/fair/`）

Fair 计算**必须**从绘图层彻底剥离，一产品一文件：

```
review_plot/fair/
├── __init__.py                # 入口：get_fair(product, ob_wide, **kwargs) -> pl.DataFrame[timestamp, product, fair]
├── base.py                    # 公共工具：pick_max_vol_price / forward_fill / wall_mid
├── ASH_COATED_OSMIUM.py       # 实现 compute_fair(ob_wide) -> DataFrame
└── INTARIAN_PEPPER_ROOT.py    # 同上
```

规则：

- 每个产品文件**只**暴露一个函数：`compute_fair(ob_wide: pl.DataFrame, **params) -> pl.DataFrame`，返回列 `[timestamp, product, fair]`。
- 参数（如 `vol_threshold=20.0`、`slope=0.001`、`intercept=13000`）作为函数默认值暴露，方便 notebook 里覆盖。
- `review_plot/fair/__init__.py` 维护 `PRODUCT_FAIR_REGISTRY: dict[str, Callable]`。新增产品只需加一个文件 + 在 registry 注册一行，不动任何绘图代码。
- 缺失产品时 fallback：返回 `compute_wall_mid(ob_wide)` 的结果（即 `base.py` 里的通用实现）。
- 不要把 fair 相关的中间列（如 `fair_ash`）泄露到 context。`compute_fair` 内部消化。

示例（`ASH_COATED_OSMIUM.py` 骨架）：

```python
from .base import pick_max_vol_price, forward_fill_two_sides

def compute_fair(ob_wide, vol_threshold: float = 20.0):
    ...
    return pl.DataFrame({"timestamp": ts, "product": "ASH_COATED_OSMIUM", "fair": fair})
```

---

## 4. 图列表与绘制方法

所有绘图函数的签名都是 `(ctx: ReviewContext, *, product: str | None = None, **opts) -> go.Figure`，内部完全从 `ctx` 取数，不再自己 IO。

### 4.0 主图 / Normalized 图的双视图约定（**重要**）

主复盘图和 Normalized 复盘图**都**必须以一对视图同时给出，缺一不可：

| 视图 | 时间范围 | Resample | 用途 |
|---|---|---|---|
| **Overview** | 全区间（无 `ts_range`） | `enable_plotly_resample(max_points=...)` **开启** | 一屏看完整天，找问题发生的大致时刻 |
| **Zoom** | 由 `ts_range=(ts_min, ts_max)` 限定 | **关闭**（全量 plotly，不采样） | 在问题时刻附近看所有原始 tick、每一笔成交、每一个挂单 |

实现方式：每个绘图函数接收 `ts_range: tuple[int|None, int|None] = (None, None)` 和 `full_resolution: bool = False` 两个参数。
- `full_resolution=True` 时函数内部临时 `disable_plotly_resample()`，绘制完再 `enable_plotly_resample()` 还原（或者用 context manager `resample.no_resample()` 包住 `fig.show()` 调用）。
- `full_resolution=False` 且全局 resample 开启时走降采样路径。
- 两个 helper 统一封装在 plots 层：

```python
def plot_main_overview(ctx, product):     # resample 开，ts_range=None
    return plot_main_review(ctx, product=product, ts_range=(None, None), full_resolution=False)

def plot_main_zoom(ctx, product, ts_range):   # resample 关，ts_range 指定
    return plot_main_review(ctx, product=product, ts_range=ts_range, full_resolution=True)
```

Normalized 图同理，提供 `plot_normalized_overview` / `plot_normalized_zoom`。

Notebook 约定的调用形如：

```python
# 全局 overview —— 先看全貌
plot_main_overview(ctx, "ASH_COATED_OSMIUM").show()
plot_normalized_overview(ctx, "ASH_COATED_OSMIUM").show()

# 针对 overview 里发现的异常窗口，打开 zoom 看细节
ZOOM = (42000, 58000)
plot_main_zoom(ctx, "ASH_COATED_OSMIUM", ZOOM).show()
plot_normalized_zoom(ctx, "ASH_COATED_OSMIUM", ZOOM).show()
```

### 4.1 图清单

| # | 图 | 文件 | 方法要点 |
|---|---|---|---|
| 1 | **主复盘图 Overview**（3 行，全区间 + resample）| `plots/main_review.py::plot_main_overview` | `make_subplots(rows=3, shared_xaxes=True, row_heights=[0.55, 0.2, 0.25])`。Row1 按 4 层顺序加 trace：Bids → Asks → Cross-Fair → Near-Fair → Fair line → Mkt Trades → My Buy → My Sell。Row2 `position_df` + `add_hline(0)`。Row3 `pnl_by_product` + `add_hline(0)`。layout: `height=900, width=1200, hovermode='x unified'`。|
| 2 | **主复盘图 Zoom**（3 行，`ts_range` 内全量）| `plots/main_review.py::plot_main_zoom` | 同上布局与 trace 顺序；`full_resolution=True` 绕开 resample；所有 DataFrame 先 `filter_by_timestamp(ts_min, ts_max)` 再喂给 markers。|
| 3 | **Normalized 复盘图 Overview**（3 行，全区间 + resample）| `plots/normalized_review.py::plot_normalized_overview` | 和主图相同布局，Row1 y 改用 `price - fair`；fair 基准线改成 `add_hline(y=0)`；新增副轴 `yaxis4` 绘 Fair Trend 虚线。`row_heights=[0.7, 0.2, 0.25]`。|
| 4 | **Normalized 复盘图 Zoom**（3 行，`ts_range` 内全量）| `plots/normalized_review.py::plot_normalized_zoom` | 与 Overview 同，`full_resolution=True` + `ts_range` 裁剪。|
| 5 | **Edge 散点时序**（Step 5b）| `plots/edge_scatter.py` | `make_subplots(rows=len(products), shared_xaxes=True)`。每个产品一行，点色 `green/gray/red` 依据 `edge > 0 / == 0 / < 0`。`add_hline(y=0)`。|
| 6 | **成交档位直方图**（Step 5c）| `plots/fill_histogram.py` | `make_subplots(rows=1, cols=len(products))`，每列一产品。buy 绿 / sell 红叠加（`barmode='overlay'`），`add_vline(x=0)`。|
| 7 | **成交间隔直方图**（Step 6）| `plots/trade_interval.py` | 每产品一列，对 `my_trades.sort('timestamp').diff()` 画直方图（`nbinsx=40`）。同时打印 mean / median / max / max_gap 时刻。|
| 8 | **PnL 归因**（Step 7）| `plots/pnl_attribution.py` | 每产品一行。绘 `cumsum(edge_pnl)`（绿 line+markers）+ `profit_and_loss`（橙 line）。打印 `cum_edge / total_pnl / mtm_component`。|
| 9 | **汇总统计**（Step 8）| `plots/summary_table.py` | 返回 `pl.DataFrame`（不是 go.Figure）。列：`trades, total_vol, pos_edge, zero_edge, neg_edge, avg_edge, cum_edge_pnl, final_pnl, mtm_component, max_pos, min_pos, final_pos, trades_per_1k_ts`。|

每个文件内部**只做两件事**：从 `ctx` 选数 → 用 `markers.py` 的 builder 装 trace。所有
marker 的颜色 / 形状 / size 映射都在 markers 层统一，不在 plots 层写魔法数字。

### 4.2 建议追加的复盘辅助标记 / 功能

下面这些不在原 notebook 里，但对"找到问题点"很有价值，建议一并纳入 markers 层，
在 Overview / Zoom 图上默认开启，可由开关关闭：

| 编号 | 名称 | 类型 | 作用 | 建议实现 |
|---|---|---|---|---|
| A | **Best Bid / Ask Band** | Row1 填充区 | 以 `bid_price_1` 和 `ask_price_1` 画半透明填色带（normalized 图用 `bid_1 - fair` / `ask_1 - fair`），一眼看出 spread 与其突变 | `go.Scatter` + `fill='tonexty'`，opacity=0.12 |
| B | **My Resting Quote Trail** | Row1 虚线 | 基于策略侧记录（若可得）或推断：每个 tick 我方尚未成交的挂单价格随时间的轨迹，用灰色细虚线；一旦成交接 My Buy/My Sell marker | 需要 trader 侧把 `orders_out` 写进 log；没有就跳过 |
| C | **Missed Opportunity**（吃掉我方应吃的市场成交） | Row1 黑空心圆 | 在 normalized 图上标记"市场成交方向与我方朝向一致但我方没有跟进"的点：`mkt_buy` 且 `delta > 0`（卖方报价高于 fair，我们本该卖但没卖） | `filter(mkt.delta > near_fair_tick & mkt.side==buy)` 用 `symbol='circle-open'` |
| D | **Adverse Selection**（成交后价格立即反向） | My Buy/Sell 红色外框 | 我方成交后 `k` 个 tick 内 fair 向不利方向移动 ≥ 阈值的标记（toxic fills） | 新增列 `fair_move_after_k(k=5)`，在 marker 层给不利 fills 加 `line.color='crimson', line.width=2` |
| E | **Drawdown Shading** | Row3 红色填色 | PnL 曲线的 drawdown 期（`pnl - cummax(pnl) < 0`）用半透明红色背景填色 | `go.Scatter fill='tozeroy'` + mask |
| F | **Position Cap Lines** | Row2 水平虚线 | 在 `±position_limit` 画水平虚线（限额通常 ±20 / ±50），让触顶 / 触底可视化 | `add_hline(±limit, line_dash='dot')`；`ctx` 新增 `position_limits: dict[str,int]` |
| G | **Fair Uncertainty Band** | Row1 半透明填充 | 对有 forward-fill / 回退 fair 的产品，在 fair 缺乏 primary 数据（走 fallback）的 tick 上画浅绿背景；提示 fair 这段可信度低 | `fair/<product>.py` 额外返回 `fair_source` 列（`primary/fallback/ffill`） |
| H | **Zoom 窗口对齐用的 Overview 上的阴影** | Overview Row1 灰色背景 | 调用 `plot_main_overview` 时可传 `highlight_ranges=[(a,b), ...]`，把 Zoom 查看的时间窗在 overview 上高亮，方便图并排看 | `fig.add_vrect(x0=a, x1=b, fillcolor='lightgrey', opacity=0.25)` |
| I | **Order Book Imbalance**（可选 Row4）| 新增子图 | `(bid1_vol - ask1_vol) / (bid1_vol + ask1_vol)` 的时序线，和 My Buy/Sell 在时间上对齐；正值通常预示下一 tick fair 上行 | `ctx` 新增 `imbalance_df`；`plot_main_review(..., show_imbalance=True)` 时 rows=4 |
| J | **关键事件注释**（vline + text） | Row1 垂直线 | notebook 可传 `annotations=[(ts, "limit hit"), ...]`，绘图函数用 `add_vline` + `annotation_text` | Zoom 视图特别有用 |
| K | **同步十字准星** | layout | `hovermode='x unified'` 已开，但 Zoom 图建议补 `spikes` (`xaxis.showspikes=True, spikemode='across'`) —— 悬停时纵向一条线贯穿三行子图，便于对齐读数 | `fig.update_xaxes(showspikes=True, spikemode='across', spikesnap='cursor')` |
| L | **Buy vs Sell Edge 分拆的累计曲线**（Step 7 增强） | PnL 归因图 | 在 `cum_edge` 之外再画 `cum_edge_buy` / `cum_edge_sell` 两条细线，揭示两侧 edge 是否对称 | group_by(trade_type).cumsum |
| M | **Tick-Level Fair Delta Histogram**（Step 5c 补充） | 新列 | 除了 buy/sell 档位分布，叠加"所有市场成交"的 `price - fair` 分布作对照；我方分布偏左 / 偏右一眼可见 | 同列 hist，灰色 `mkt_trades.delta` 作参照 |

对应开关统一加入 5.8 的 `ReviewContext.from_submission` 或绘图函数 kwargs：

```python
ReviewContext.from_submission(
    ...
    position_limits: dict[str, int] | None = None,      # F
    adverse_lookahead_ticks: int = 5,                   # D
    adverse_fair_move_threshold: float = 1.0,           # D
)

plot_main_overview(ctx, product,
    show_spread_band: bool = True,                      # A
    show_missed_opportunity: bool = True,               # C
    show_adverse_fills: bool = True,                    # D
    show_drawdown: bool = True,                         # E
    show_position_caps: bool = True,                    # F
    show_fair_uncertainty: bool = True,                 # G
    highlight_ranges: list[tuple[int,int]] | None = None,  # H
    show_imbalance: bool = False,                       # I
    annotations: list[tuple[int,str]] | None = None,    # J
    show_spikes: bool = True,                           # K
)
```

> 建议优先实现 **A / E / F / H / K**（几乎零额外数据依赖，ROI 最高），
> 随后是 **D / L / M**（依赖 `my_with_fair` 已有字段），
> 最后是 **B / C / G / I / J**（需要 trader 侧 log 或额外计算）。

---

## 5. 开关与过滤模块

全部开关集中在 `ReviewContext.__init__` 的 kwargs + `resample.enable_plotly_resample`。
`filters.py` 提供纯函数给 context 使用，也可在 notebook 里单独调用调试。

### 5.1 Plotly resample 开关

```python
# review_plot/resample.py
def enable_plotly_resample(max_points: int = 4000) -> None: ...
def disable_plotly_resample() -> None: ...
```

- **含义**：劫持 `go.Figure.show`，对 `scatter` / `scattergl` trace 做均匀下采样（保留首尾点），同步 `x/y/customdata/text/hovertext/marker.size/marker.color/line.color` 所有逐点字段，避免长度错位。
- **参数**：
  - `max_points: int`（默认 4000）—— 每个 scatter trace 的目标点数上限。≤ 这个数不采样。
- **副作用**：全局幂等（重入保护 `_orig_show_for_resample`）。调 `disable_plotly_resample()` 会恢复原函数。

### 5.2 按量过滤（qty whitelist）

```python
# review_plot/filters.py
def filter_trades_by_qty(df: pl.DataFrame, allowed_qty: set[int] | None) -> pl.DataFrame: ...
def filter_orders_by_qty(df: pl.DataFrame, allowed_qty: set[int] | None, abs_value: bool = True) -> pl.DataFrame: ...
```

- **含义**：只保留量落在白名单里的 trade / order（order 用 `abs(volume)`）。
- **参数**：
  - `allowed_qty: set[int] | None`：白名单。`None` 表示不过滤（默认）。传 `set()` 则全部过滤掉。
  - `abs_value: bool`：orderbook 场景下成交量取绝对值比较，默认 True。
- **入口**：`ReviewContext(qty_filter=...)`，过滤在 context 构造完成后应用到 `my_trades / mkt_trades / ob_long` 三份数据，保证所有下游图一致。

### 5.3 挂单异常 / 邻近过滤

```python
def mark_cross_fair(ob_long: pl.DataFrame, fair_df: pl.DataFrame) -> pl.DataFrame: ...
def mark_near_fair(ob_long: pl.DataFrame, fair_df: pl.DataFrame, tick: int = 4) -> pl.DataFrame: ...
```

- **开关**：
  - `ReviewContext(cross_fair_only=False)` —— 若设 `True`，主图只画 cross-fair 异常挂单，隐藏常规 bid/ask 散点。
  - `ReviewContext(near_fair_tick=4)` —— `|price - fair|` 阈值。设 `None` 关闭 Near-Fair 高亮。

### 5.4 Edge 过滤

```python
def filter_trades_by_edge(df: pl.DataFrame, sign: Literal["pos", "neg", "zero", "nonneg"] | None) -> pl.DataFrame: ...
```

- **开关**：`ReviewContext(edge_filter=None)`；设 `"neg"` 可让 normalized 主图只显示亏损成交，便于定位负 edge。

### 5.5 时间窗口过滤

```python
def filter_by_timestamp(df: pl.DataFrame, ts_min: int | None, ts_max: int | None) -> pl.DataFrame: ...
```

- **开关**：`ReviewContext(ts_range=(ts_min, ts_max))`，默认 `(None, None)`。所有 DataFrame 统一裁剪。

### 5.6 产品过滤

- **开关**：`ReviewContext(products=None)`。`None` 表示自动 `ob_wide['product'].unique()`；传 list 限定要分析的产品子集。

### 5.7 Hover 模板开关

- **开关**：`ReviewContext(show_delta_in_hover=True)`。若关闭，hover 只显示 `qty, px`，不计算 delta。对 `qty_filter` 以后仍然成立。

### 5.8 综合签名（复制即用）

```python
ReviewContext.from_submission(
    submission_id: str,
    my_trades_dir: str | Path = "data/my_trades",
    *,
    products: list[str] | None = None,
    ts_range: tuple[int | None, int | None] = (None, None),
    qty_filter: set[int] | None = None,
    edge_filter: Literal["pos", "neg", "zero", "nonneg"] | None = None,
    cross_fair_only: bool = False,
    near_fair_tick: int | None = 4,
    show_delta_in_hover: bool = True,
    fair_overrides: dict[str, Callable] | None = None,   # 临时替换某产品的 compute_fair
) -> ReviewContext
```

`fair_overrides` 直接绕开 `fair/<product>.py`，方便研究时快速试验新 fair 定义而不改文件。

---

## 迁移验收清单

- [ ] `3_review.ipynb` 没有任何 `pl.read_csv` / `json.load` 调用，cell 数 ≤ 8。
- [ ] 所有 hardcoded fair 公式都在 `review_plot/fair/<PRODUCT>.py` 里。
- [ ] 新增一个产品只需 (a) 加一个 `fair/<PRODUCT>.py`，(b) 在 registry 登记一行；**无需动绘图代码**。
- [ ] 所有订单 / 成交 marker 的 hover 都含 `qty, px, delta`（delta 受 5.7 开关控制）。
- [ ] 打开 `qty_filter={1,2,5}` 后，主图、normalized、edge scatter、histogram、summary 表里的样本量全都同步收缩。
- [ ] 关掉 `enable_plotly_resample()` 后，图点数 = 原始点数；打开后任意 scatter trace 点数 ≤ `max_points`。
