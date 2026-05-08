# IMC Prosperity 4 — Algorithmic Trading Retrospective

<div align="center">

  <a href="https://prosperity.imc.com/leaderboard">
    <img src="https://img.shields.io/badge/Official-Leaderboard-1A1B27?style=for-the-badge&logo=google-analytics&logoColor=white" height="45">
  </a>
  <a href="https://prosperity.imc.com/leaderboard">
    <img src="https://img.shields.io/badge/Algo_Rank-96th-FFD700?style=for-the-badge" height="45">
  </a>
  <a href="https://prosperity.imc.com/leaderboard">
    <img src="https://img.shields.io/badge/Overall_Rank-93rd-blue?style=for-the-badge" height="45">
  </a>

</div>

> This is a **methodology-showcase repository**, not a champion's highlight reel. Our **final overall rank was 93rd, algorithmic-trading rank 96th — Top 0.5%** (see the rank summary below). The purpose of open-sourcing this is not to flaunt rankings but to fully document **how we approached an unfamiliar market from scratch — observing, hypothesizing, modeling, backtesting, and reviewing** — including which steps we got right and which alphas we missed because of preconceived assumptions.
>
>
> **If you're short on time, jump directly to the [Round 1](#round-1--single-asset-trend--single-asset-mean-reversion) and [Round 5](#round-5--50-assets-complex-cross-asset-structure) retrospectives.** Round 5 is especially worth reading: we independently verified the mathematical structure of a microstructure phenomenon through three different methods, yet judged it "no alpha here" because of two ingrained assumptions. We only realized in hindsight that this was the largest tradable edge of the round.

---

## 🌌 Team

<div align="center">
  <a href="prosperity/assets/images/DarkForestHunter_TeamName.png">
    <img src="prosperity/assets/images/DarkForestHunter_TeamName.png" alt="Dark Forest Hunter Team Logo" width="100%">
  </a>
</div>

<details>
  <summary align="center">
    <b>🏹 [CLICK TO VIEW TEAM POSTER]</b>
  </summary>
  <p align="center">
    <br>
    <img src="prosperity/assets/images/DarkForestHunter_Poster.png" width="85%" alt="Dark Forest Hunter Poster">
    <br>
    <i>"The universe is a dark forest. Every civilization is an armed hunter..."</i>
  </p>
</details>

<br>

<table>
<tr>
<td width="180" align="center">
<img src="prosperity/assets/images/leo_photo.png" width="140" style="border-radius:50%"/><br/>
<b>Haoqing Liu (Leo)</b>
</td>
<td>
<b>Strategy Design · Backtesting Framework · Retrospective Visualization</b><br/><br/>
Responsible for overall strategy design, microstructure analysis, the in-house backtesting framework and hyperparameter search, the retrospective visualization module, and the bulk of the statistical modeling work.<br/><br/>
🔗 <a href="https://www.linkedin.com/in/haoqing-liu-2232b2293/">LinkedIn</a> · 📧 liuhaoqing.leo@gmail.com · 💻 <a href="https://github.com/Leo-Hawking">GitHub</a>
</td>
</tr>
<tr>
<td width="180" align="center">
<img src="prosperity/assets/images/mike_photo.jpg" width="140" style="border-radius:50%"/><br/>
<b>Zhuoqin Peng (Mike)</b>
</td>
<td>
<b>Data Analysis · Cross-Asset Relationship Mining</b><br/><br/>
Responsible for the statistical exploration and validation of cross-asset relationships. The core discoveries of Round 5 — the negative-correlation structure within the PEBBLE group and the positive/negative-correlation and reversal relationships within the SNACK group — were primarily driven by his analysis of the 50-asset return correlation matrix.<br/><br/>
🔗 <a href="https://www.linkedin.com/in/mike-peng-244237245/">LinkedIn</a>
</td>
</tr>
</table>

---

## About the Competition

IMC Prosperity 4, hosted by IMC Trading, is divided into algorithmic and manual trading. This repository focuses on the algorithmic trading portion.

The rules: based on three days of microstructure data (order book snapshots and trade records) provided by the organizers, write a Python strategy file and submit it to the official matching engine to run online, with the goal of maximizing PnL.

The format: 1 Tutorial round + 5 official rounds. Phase 1 (Rounds 1–2, 72 hours each) and Phase 2 (Rounds 3–5, 48 hours each), with rankings reset between phases.

A total of **18,803** teams participated.

<div align="center">
  <img src="prosperity/assets/images/competition_scale.jpeg" alt="Competition Scale and Overview" width="100%">
</div>

Official materials: <https://imc-prosperity.notion.site/prosperity-4-wiki>

---

## Rank Summary

| Round | Market Theme | Algo Rank | Key Takeaway |
|---|---|---|---|
| Round 0 | Tutorial (matching-engine probing) | — | Confirmed via probe orders that "no queue, bots only hit best, taker can cross" — the foundational mechanics |
| Round 1 | Single-asset trend + single-asset mean reversion | **170** | Two-layer fair-price correction took shape; we didn't load up on ASH's extreme-deviation regions, where top teams' hard-coded logic widened the gap |
| Round 2 | Same as Round 1 + sealed-bid auction game | **77** | Bidding hedged uncertainty; ASH's mean drifted, and our conservative position management turned out to be advantageous |
| Round 3 | Options (10 strikes) + spot | **95** | Abandoned vol/pairs approaches in favor of unified mean reversion across all assets — interpretability over precision |
| Round 4 | Same market as Round 3 + bot names disclosed | **132** | Bucketing analysis identified Mark 14 / Mark 55 as informed bots; strategy-migration inertia caused a slip |
| Round 5 | 50 assets, complex cross-asset structure | **96** | Confirmed the "inner/outer orders are generated separately" hypothesis; missed the high-frequency mean-reversion alpha in round-100 jump regions |

---

## ⚠️ A Note on Code Runnability

Most of the code in this repo is **the version we actually used during the competition**. Because of time pressure, we did not refactor for open-source presentation — **running it directly may require fixing paths and dependencies** (hard-coded paths, missing dependencies, leftover debug code, etc.).

If you are a potential employer or collaborator, **the core value of this repository lies in the strategy reasoning, the research documents (`*.md`), the retrospective visualization framework, and the per-round methodological narrative** — not in being a plug-and-play codebase. I'm happy to walk through specific implementation details in interviews.

---

## Table of Contents

- [Workflow](#workflow)
- [Repository Map](#repository-map)
- [Infrastructure & Tools](#infrastructure--tools)
- [Round-by-Round](#round-by-round)
  - [Round 0 — Tutorial](#round-0--tutorial)
  - [Round 1 — Single-Asset Trend + Single-Asset Mean Reversion](#round-1--single-asset-trend--single-asset-mean-reversion)
  - [Round 2 — Sealed-Bid Auction Game](#round-2--sealed-bid-auction-game)
  - [Round 3 — Options + Spot](#round-3--options--spot)
  - [Round 4 — Bot Names Revealed, Follow-Trading Mining](#round-4--bot-names-revealed-follow-trading-mining)
  - [Round 5 — 50 Assets, Complex Cross-Asset Structure](#round-5--50-assets-complex-cross-asset-structure)
- [Overall Retrospective](#overall-retrospective)

---

## Workflow

We drew inspiration from publicly shared writeups of past competitions (mainly [TimoDiehm/imc-prosperity-3](https://github.com/TimoDiehm/imc-prosperity-3)). Our overall flow is a loop of **"probe → visualize → hypothesize → spec doc → implement → backtest & retrospect"**:

1. **Probe strategy** — At the start of each round, we submit a few empty strategies and mechanical grid-quoting orders to map out the current market's matching mechanics (queueing, tick size, cross-price behavior, etc.). Code in `prosperity/round0trade/`.

2. **High-density visualization** — Once we have the data, the first thing we do in a notebook is overlay multiple layers of information (order book, trades, our own quotes and fills, inner/outer spreads) on a single chart. We use `plotly` because it lets us zoom into microstructure-level time scales. Research notebooks live in `prosperity/research_round12/`, `prosperity/round3research/`, `prosperity/round4research/`, and `prosperity/round5research/`.

3. **Hypothesis verification** — Intuitive observations from the charts are then verified statistically from multiple angles (e.g., in Round 1, we used AR(1) fitting + ACF half-life cross-checking to verify the OU hypothesis), to minimize "see-and-tell" statistical illusions.

4. **Spec docs before code** — Once an idea is confirmed, I iterate with an AI to manually write the strategy rules into `.md` documents (every module's details, edge cases, and fallback paths spelled out clearly). The point of this step is to **force myself to articulate every branch of the strategy and the source of every parameter** — this is the foundation of strategy interpretability and debuggability. Implementation is delegated to Claude Code, given the time pressure.
   - Examples: `prosperity/round1trade/strategy_unified.md`, `prosperity/round5trade/PEBBLES_做市与对冲算法规范.md`

5. **Backtest & retrospective** — Each version is run through our in-house backtest framework (`prosperity/backtest/`) to produce a fill log, then the retrospective visualization module (`prosperity/review_plot/`) plots fills, PnL attribution, edge scatter, etc. Based on these, we decide between **iterating the version** or **hyperparameter optimization** (which we do by preferring parameter plateaus over global maxima, for robustness).

> **The limitations of this workflow were also fully exposed during the competition** — see the [Overall Retrospective](#overall-retrospective). In short: a two-person discussion (human + AI) easily reaches consensus around mistaken beliefs (Round 5 was exactly this), and breaking out of that requires more independently-alpha-generating people in adversarial discussion.

---

## Repository Map

> **A note on naming**: filenames mix Chinese and English, and several rounds contain duplicated `final.py / v2 / v3` naming. This is mainly due to (a) time pressure and (b) the fact that strategy iteration was tree-shaped rather than linear (we explored multiple directions in parallel for the same asset). We kept the original naming to faithfully reflect the iteration process.

```
prosperity/
├── backtest/                     # In-house backtest framework + hyperparameter search (per-round + generic)
├── configs/                      # Default config
├── data/bt/                      # Three days of market data + trades provided by organizers (per round)
│
├── research_round12/             # Round 1–2 research notebooks (strategy dev, ASH inversion, Pepper microstructure, etc.)
├── round3research/               # Round 3 research (options, vol, mean reversion validation)
├── round4research/               # Round 4 research (bot behavior analysis, signal validation)
├── round5research/               # Round 5 research (ETF regression, Pebble pairing, market exploration)
│
├── round0trade/                  # Tutorial probe strategies (grid_probe / robber / hardcoded / test)
├── round1trade/                  # Round 1 strategies (ash / root / pepper iterations + spec docs)
├── round2trade/                  # Round 2 strategies (R1 base + bid auction logic)
├── round3trade/                  # Round 3 strategies (aggressive / linear-rebalance / making / mean-reversion)
├── round4trade/                  # Round 4 strategies (slow-follow / follow_2state / aether, etc.)
├── round5trade/                  # Round 5 strategies (Pebble/Snack/generic making, with spec docs)
│
├── review_plot/                  # Retrospective visualization framework (modular: context / fair / plots / markers)
├── vev_plot/                     # Options-specific visualization (IV surface / moneyness / strike arb)
├── utils/                        # Shared utilities (dataio, fair, orderbook, viz)
│
└── assets/images/                # README images
```

---

## Infrastructure & Tools

We invested significant time in infrastructure because the markets vary widely round-to-round, and **reusable tooling is the highest-marginal-return investment**:

| Module | Problem Solved | Design Trade-offs |
|---|---|---|
| `prosperity/backtest/` | Reproduce the official matching engine + run hyperparameter searches | A shared core across all five rounds, plus per-round search scripts (`round4_2d.py`, `hydrogel_zsearch.py`, etc.) to avoid changing the generic entry point each time. The cost is that the core code grew bloated round by round — see [Overall Retrospective](#overall-retrospective) |
| `prosperity/review_plot/` | Overlay fill log + market data + fair price into a single chart, do PnL attribution | Modular split (`fair/` for fair-value computation, `plots/` for charts, `markers.py` for event annotation). Each asset can register its own fair-value calculator |
| `prosperity/vev_plot/` | After Round 3 introduced options, we needed dedicated views for IV surface, moneyness, and inter-strike arbitrage | Mirrors `review_plot/`'s structure but is decoupled — to keep options-specific logic from polluting the generic framework |
| `prosperity/utils/` | Data I/O, order book construction, fair-price utilities, visualization primitives | The minimal shared kernel reused across rounds |

One thing worth calling out separately is the **PnL attribution** in `review_plot/` (`pnl_attribution.py` + `pnl_decomp.py`): it decomposes each trade's PnL into spread profit, position drift, rebalancing cost, etc. This is our core tool for judging whether a strategy is genuinely earning edge or just riding a tailwind.

---

## Round-by-Round

### Round 0 — Tutorial

**Goal**: Map out the organizers' matching mechanics.

We submitted a grid-quoting probe strategy and confirmed several mechanical rules that turned out to be critical for every subsequent round:

- **No queueing**: orders posted at the best price are processed by timestamp regardless of when they were placed — there is no "I'm behind in the queue at the best price" situation.
- **Bots only hit best price**: market bots will not actively cross the best price even when depth is shallow.
- **No two trade prices at the same timestamp**: bot-initiated trades at the same timestamp can only occur at the best price level.
- **Active take can cross**: there is no such restriction on our side — an active take can punch through multiple levels in one go.

> **Direct corollary (affecting every subsequent round)**: posting at the best price requires no consideration of queue priority — being "1 unit better than the market best" is enough to intercept all favorable bot-initiated trades. This became the default skeleton of every subsequent market-making strategy.

**Relevant files**: `prosperity/round0trade/trader_grid_probe.py`, `prosperity/round0trade/trader_hardcoded.py`, `prosperity/round0trade/trader_robber.py`

---

### Round 1 — Single-Asset Trend + Single-Asset Mean Reversion

**Final algo rank: 170**

**Assets**: `INTARIAN_PEPPER_ROOT` (ROOT) and `ASH_COATED_OSMIUM` (ASH), position limit ±80.

#### Market Observation

<p align="center">
  <img src="prosperity/assets/images/round1_microstructure_overview_root.png" width="48%"/>
  <img src="prosperity/assets/images/round1_microstructure_overview_ash.png" width="48%"/>
</p>

<p align="center"><i>Round 1 — Microstructure of ROOT (left) and ASH (right). ROOT is strictly linear and monotonically increasing; ASH exhibits clear mean reversion with a wide spread.</i></p>

After plotting, we found that the two assets behaved very differently at the microstructure level: **ROOT is essentially deterministic linear growth, while ASH mean-reverts clearly around 10000**.

A more important observation was about the order book itself: **the outer layers contain large orders while the inner layers are sparsely populated by small orders**. This suggests the market generation mechanism may be: first set an implicit fair price, post outer large orders around it, and later inject inner small orders. Using the midpoint of best bid/ask directly as the fair price would treat inner-order noise as price changes.

This led us to define a **two-layer fair-price calculation** (reused in many subsequent rounds):

1. **Raw fair price**: midpoint of the largest bid and largest ask; if either side has no order above an integer threshold `n`, forward-fill.
2. **Corrected fair price**: use inner orders to denoise the raw fair price further — pull all inner orders to within ±2 of the fair price.

<p align="center">
  <img src="prosperity/assets/images/round1_normalized_root.png" width="48%"/>
  <img src="prosperity/assets/images/round1_normalized_ash.png" width="48%"/>
</p>

<p align="center"><i>Round 1 — Inner-order distribution after normalization (ROOT left, ASH right). After subtracting raw fair price from every marker, ASH's inner orders fall almost exactly on +1.5 and −2.5 — meaning the fair price can be further corrected by the inner orders.</i></p>

#### Hypothesis

- **ROOT**: strictly linear and monotonically increasing.
- **ASH**: generated by an OU process with a long-run mean around 10000.

For ASH we ran a complete statistical validation pipeline:

1. **Series reconstruction**: use the two-layer method above to recover the inner fair-price series.
2. **AR(1) fitting**: discretize the continuous OU process into AR(1) and use linear regression to extract the long-run mean and mean-reversion speed.
3. **ACF cross-validation**: compare the theoretical half-life implied by AR(1) with the exponential decay rate of the actual ACF — verifying the OU property.
4. **Noise extraction**: pull out the Brownian term to determine the baseline volatility.
5. **Signal construction**: build a z-score based on the actual variance of the stationary distribution.

<p align="center">
  <img src="prosperity/assets/images/round1_ash_meanrev_scatter.png" width="70%"/>
</p>

<p align="center"><i>Round 1 — ASH mean-reversion validation. X-axis: deviation from 10000 (binned); Y-axis: average next-step price change. Except for sparse-sample regions at the extremes, the data falls strictly on a line with negative slope — confirming the OU hypothesis.</i></p>

The result: the OU property is significant, but **noise dominates** — this was the key reason for choosing conservative position management later.

#### Strategy Choice and Trade-offs

**ROOT strategy — two-stage:**

Initially, we wanted both "passive market-making + capturing drift via positions" and designed a monotonically increasing target-position function. Two problems surfaced quickly:

1. **Passive quoting rebalances too slowly**: active fills are far sparser than passive quotes, so passive position-building would miss the early advantageous prices.
2. **The target-position function saturates almost instantly**: it caps out immediately, after which it can no longer differentiate trading actions and instead introduces many arrival-rate parameters that hurt robustness.

→ Switched to two stages:

- **Stage 1 (rapid position-building)**: this stage drives most of the PnL. We wrote a lot of redundant logic to rapidly anchor the opening fair price (handling all kinds of one-sided-missing edge cases), then set a cross-price threshold — only taking orders priced better than `fair_price + threshold`, rather than blindly taking. This gives us both **building speed** and **execution price**.
- **Stage 2 (harvesting spread)**: once near full position, switch to making. Fixed thresholds: when full, post passive sell quotes; once filled and total position drops below threshold, immediately take an inner buy or post a passive buy to refill — to maximize capture of the positive drift.

**ASH strategy — three-tier:**

Based on the optimal-position function from the OU model, partitioned into three tiers by **how far the current position deviates from the optimum**:

| Deviation tier | Outer logic | Inner logic |
|---|---|---|
| Small | Two-sided quoting (wide spread, the main source of PnL) | Two-sided take of irrational orders crossing fair price |
| Medium | Two-sided quoting | One-sided take (gives up some inner-take profit for tighter risk exposure) |
| Extreme | One-sided passive quote | One-sided take (rare) |

After both products were implemented, we ran hyperparameter optimization on the in-house backtest (preferring parameter plateaus over global maxima) and submitted.

#### Result and Retrospective

**Rank 170.**

In retrospect, the main gap came from **ASH's extreme-deviation regions**: top teams **hard-coded aggressive position-building logic for extreme deviations** and captured an extra slug of PnL during a violent move, directly widening the gap.

> **Key reflection**: our OU model was statistically correct, but applying a single parameterized optimal-position rule to all deviation regions was too uniform — in the sparse-sample tail, the model's own uncertainty actually permits more aggressive manual intervention. Next time we will explicitly split "statistics-driven" and "manually-intervened regions" into two layers.

**Key files**:
- Strategy: [`prosperity/round1trade/final.py`](prosperity/round1trade/final.py)
- ASH sub-strategy: [`prosperity/round1trade/final_ash.py`](prosperity/round1trade/final_ash.py), [`prosperity/round1trade/new_ash_strategy.md`](prosperity/round1trade/new_ash_strategy.md)
- ROOT sub-strategy: [`prosperity/round1trade/final_root.py`](prosperity/round1trade/final_root.py), [`prosperity/round1trade/root_fair_calculate.md`](prosperity/round1trade/root_fair_calculate.md)
- Research notebook: [`prosperity/research_round12/ash_fair_reverse.ipynb`](prosperity/research_round12/ash_fair_reverse.ipynb)

---

### Round 2 — Sealed-Bid Auction Game

**Final algo rank: 77**

#### Market Observation

Same assets as Round 1, with a new mechanic: **every team submits a bid, and the top 50% of bidders receive an extra 25% of market-bot fills, with the bid amount deducted from total PnL**.

We also noticed that the new bots **seemed to embed a price-change signal** — but after detailed validation, its stability was insufficient to be a tradable alpha.

#### Hypothesis

The market structure should persist: ROOT continues linear, ASH continues to mean-revert with the same parameters.

#### Strategy Choice and Trade-offs

**Bid decision**: this is an incomplete-information game. On our backtest we compared "with extra fills" vs. "without" — the edge PnL difference was substantial across the two days (~800 on one day, nearly 2000 on the other).

We saw on Discord that a sizable fraction of teams claimed they would bid 0 or single digits, so our initial price was **102** (accounting for 100/101 clustering effects). Reweighing the risk afterward: **if we miss out, we lose 1000+ in expected return**. So we ultimately raised it to **151** — paying an extra 50 to deterministically secure the extra fills, treating the 50 as a risk premium.

The strategy itself was carried over from Round 1.

#### Result and Retrospective

**Rank 77 (a significant jump).** In fact, even bidding 100 would have won — the extra 50 we paid didn't change the outcome, but it was an acceptable cost ex ante.

**What truly determined the ranking wasn't the bid**, but something else: **ASH's mean was no longer 10000 — there was a clear downward drift**.

<p align="center">
  <img src="prosperity/assets/images/ash_trade_result_round2.png" width="80%"/>
</p>

<p align="center"><i>Round 2 — ASH actual-fill review. The price center has clearly shifted down compared to Round 1. Our trend-conservative trading style lost some opportunities and increased risk exposure under the distributional drift, but the loss was smaller than for similarly-ranked teams.</i></p>

<p align="center">
  <img src="prosperity/assets/images/normalize_root_trade_result_round2.png" width="80%"/>
</p>

<p align="center"><i>Round 2 — ROOT trading result in the normalized view. The two-stage "rapid build + full-position making" pattern is clearly visible.</i></p>

Our mean-reversion strategy keeps positions relatively conservative and trades two-sided at high inventory — on the surface this increases variance and gives up some trend opportunities. But the top teams from Round 1, with their hard-coded aggressive extreme-deviation logic, **suffered worse precisely because they "loaded up to the max"** when ASH's mean drifted.

> **This wasn't us being right and them being wrong — it was that our conservatism happened to hedge a mean-drift risk we hadn't actually identified. We logged this as a "lucky escape"**: it shows that we lacked continuous monitoring of parameter stability (was `mu` really constant at 10000?). Next time, we'll explicitly add a distribution-drift detector.

**Key file**: [`prosperity/round2trade/final2.py`](prosperity/round2trade/final2.py)

---

### Round 3 — Options + Spot

**Final algo rank: 95 (after Phase 2 reset)**

#### Market Observation

Phase 2 begins, options are introduced — this is a relatively unfamiliar domain for us, and each round is now compressed to 48 hours.

**Assets**:
- Spot: `HYDROGEL_PACK` (PACK), `VELVETFRUIT_EXTRACT` (FRUIT), position limit ±200.
- 10 strikes of FRUIT call options: VEV_4000 (deep ITM, price ≈ FRUIT − K), VEV_4500, VEV_5000, VEV_5100, VEV_5200, VEV_5300, VEV_5400, VEV_5500 (near ATM), VEV_6000, VEV_6500 (deep OTM, price 0, cannot be bought). Position limit ±300 each.

Only PACK, FRUIT, and VEV_4000 had two-sided active fills; the rest had either one-sided or no active fills. **Market-making opportunities were substantially reduced — managing positions required active taking** — a structurally different setting from the previous rounds.

#### Evolution of the Hypothesis

We tried three directions; the first three were rejected:

1. ❌ **Implicit ETF relationship** (is PACK a composite of certain options?): regression R² too small — rejected.
2. ❌ **PACK = β·FRUIT + Brownian noise**: synchronous early, divergent later, but too many parameters and insufficient evidence to convert into alpha.
3. ❌ **Volatility surface arbitrage**: we plotted the IV surface (see `prosperity/vev_plot/`) and fitted after removing outlier strikes, but found IV deviated from the surface for long periods, with reversion speed too slow to cross the spread. Full IV-surface construction and fitting are in [`prosperity/round3research/option_research.ipynb`](prosperity/round3research/option_research.ipynb).
4. ✅ **Mean reversion across all assets**: ultimately we found that **every asset itself** showed clear mean reversion, with reversion speed fast enough and deviation amplitude wide enough — this became our core source of alpha.

#### Strategy Choice and Trade-offs

Mean reversion using z-score over short windows is easily distorted by insufficient warm-up data. We made two engineering decisions:

- **Skip z-score; use absolute deviation from a long-window EMA** to trigger trades (drop rolling sigma).
- **Pre-feed a reference EMA** to mitigate cold start.

On execution: for the three assets with two-sided active fills (PACK, FRUIT, VEV_4000), we layered making logic on top; for the other options, active take continues to use thresholds to avoid overtrading.

<p align="center">
  <img src="prosperity/assets/images/FRUIT_trade_round3.png" width="80%"/>
</p>

<p align="center"><i>Round 3 — FRUIT trading result. EMA-deviation-triggered mean-reversion trading kept a stable execution rhythm in a highly volatile market.</i></p>

#### Result and Retrospective

**Rank 95.** This round was extremely volatile overall — many teams running Gamma Scalping had stellar backtests but blew up live. Most teams at our level also chose to "abandon cross-asset relationships and unify on mean reversion", which **preserved strategy interpretability and avoided logical conflicts**.

> **In hindsight**: I overlooked inner-order handling. We had already verified in Round 1 on ASH that "using inner orders to correct fair price" works, **but in Round 3 I didn't bother to add this layer because the spread was small — directly giving up the zero-cost rebalancing opportunity**. Had I transferred the ASH approach to options for more accurate fair-price + inner take, holding PnL would have been notably more stable. This is a failure of my own workflow.

**Key files**:
- Strategy: [`prosperity/round3trade/激进_best.py`](prosperity/round3trade/激进_best.py)
- Options research: [`prosperity/round3research/option_research.ipynb`](prosperity/round3research/option_research.ipynb) (including full IV-surface construction and fitting)
- Mean-reversion validation: [`prosperity/round3research/reversion_research.ipynb`](prosperity/round3research/reversion_research.ipynb), [`prosperity/round3research/round3_validation.ipynb`](prosperity/round3research/round3_validation.ipynb)

---

### Round 4 — Bot Names Revealed, Follow-Trading Mining

**Final algo rank: 132**

#### Market Observation

The market is identical to Round 3, but **the organizers disclosed the bot names of both sides of every trade** — an obvious hint: the bot identities likely contain information.

#### Hypothesis and Validation

> **The key research approach is: don't trust "smart-looking" bots; filter by a quantitative criterion**.

Step 1: mark each bot's trades on the chart and compute each bot's holding PnL. Result: **every bot's holding PnL is a random walk around 0** — looking at PnL alone tells you nothing about who's smart.

Step 2 (the key one): **bucket by "the bot's trade price's deviation from EMA"**, and within each bucket, looking only at directionally-correct trades, compare the bot's trade price against the bucket's average trade price.

Result: **Mark 14 and Mark 55** were clearly better in different deviation regions, with Mark 55 especially consistent. These are the two genuinely informed bots. Full analysis is in [`prosperity/round4research/bot_analysis.ipynb`](prosperity/round4research/bot_analysis.ipynb) and [`prosperity/round4research/bot_recognize.md`](prosperity/round4research/bot_recognize.md).

#### Strategy Choice and Trade-offs

Layered follow-trading logic on top of the Round 3 strategy: each time we enter a tradable threshold zone, **don't open immediately — wait for Mark 14 / Mark 55 to trade, then follow**. Two parameters introduced:
- max position size to follow per trade
- after entering the trade zone, from which order index to start following

The backtest showed stable improvement in execution, but the **alpha gain was limited**.

#### Result and Retrospective

**Rank 132 (down from 95)**. PnL was clearly lower than the previous round.

> **My attribution for this drop: strategy-migration inertia + risk stacking**. Applying the same mean-reversion logic to all highly correlated assets means **every asset builds positions in the same direction simultaneously** — amplifying systematic exposure. The issue existed in Round 3 as well, but the market was friendly. In Round 4, with a slightly different structure, it surfaced.

**Key files**:
- Strategy: [`prosperity/round4trade/慢跟单.py`](prosperity/round4trade/慢跟单.py)
- Research notebook: [`prosperity/round4research/bot_analysis.ipynb`](prosperity/round4research/bot_analysis.ipynb)
- Signal validation: [`prosperity/round4research/bot_recognize.md`](prosperity/round4research/bot_recognize.md), [`prosperity/round4research/trader_signal_validation.md`](prosperity/round4research/trader_signal_validation.md)

---

### Round 5 — 50 Assets, Complex Cross-Asset Structure

**Final algo rank: 96**

#### Market Observation

10 groups, 5 assets per group, **50 assets** total. Position limit ±10 per asset. The prompt hints: **some assets may be more advantageous than others** — meaning we need to identify which of the 50 are not "plain geometric Brownian motion" and mine alpha from there.

#### Overall Research Framework

> With 50 assets, a chart-eyeballing approach will inevitably surface some that "look patterned" — this is a breeding ground for overfitting. **The discipline we set: only mine alpha actively when multiple independent pieces of evidence support it**.

We ultimately identified **three independent alphas** (two of which we exploited; the third **we incorrectly judged as untradable**):

1. ✅ **PEBBLE group's exact ETF mirror** — pair making + zero-cost inner rebalancing.
2. ✅ **SNACK group's multi-asset correlation structure** — pairing + single-asset mean reversion.
3. ❌ **High-frequency mean reversion in round-100 jump regions** — the mathematical structure was clearly identified but blocked by mistaken assumptions and not exploited.

#### Independent Validation of the Three Alphas

**Alpha 1: PEBBLE group's exact ETF mirror**

Mike built a heatmap of **return correlations** across the 50 assets:
- The XL asset in the PEBBLE group has a **−0.5 correlation** with other group members;
- The SNACK group has strong positive/negative correlation structures;
- **All other assets have return correlations strictly equal to zero**.

<p align="center">
  <img src="prosperity/assets/images/round5_corr_heatmap.png" width="75%"/>
</p>

<p align="center"><i>Round 5 — Return-correlation heatmap of the 50 assets. Only the PEBBLE and SNACK groups exhibit non-zero internal correlation structures; all other groups are strictly zero.</i></p>

I then ran regressions on the **prices themselves** for each group: **PEBBLE group's R² is exactly 1** — meaning some linear combination of the 5 assets is constant. Further investigation confirmed: **the sum of all PEBBLE-group assets equals 50000 exactly**.

This means **there's no mispricing alpha, but pairing can drastically reduce variance**.

A more striking discovery came from porting the two-layer fair-price method from Round 1: **when I pulled all inner orders strictly to the fair price, the inner-order sequences across multiple assets — within the same group, even adjacent groups — were identical** in timing, direction, and size — perfectly synchronized.

> **This finding closes the loop on the Round 1 conjecture — inner and outer orders are indeed generated separately, with inner orders inserted later**. Whether this is an organizers' easter egg or unintended information leak, it makes it possible to do **perfectly synchronized zero-cost hedging rebalances** in the PEBBLE group: holding PnL becomes essentially zero, leaving pure trading PnL.

**Alpha 2: SNACK group's correlation structure**

Mike further found: in the SNACK group, **chocolate and vanilla are strongly negatively correlated**; **strawberry ≈ inverse of raspberry + upward drift**; **pistachio ≈ inverse of raspberry + downward drift**.

I verified via regression: strawberry's volatility ≈ raspberry's, pistachio's ≈ half of raspberry's, and all three exhibit significant mean reversion.

**Alpha 3 (the missed one): round-100 price jumps**

Some assets, in certain windows, **jump on round-100 prices**, while spread and overall volatility remain unchanged. I verified this structure with three independent methods:

<p align="center">
  <img src="prosperity/assets/images/round5_jump_asset.png" width="75%"/>
</p>

<p align="center"><i>Validation (1): direct order/price chart. The price series is clearly stuck on round-100 grid points.</i></p>

<p align="center">
  <img src="prosperity/assets/images/round5_diff_of_price_plot.png" width="75%"/>
</p>

<p align="center"><i>Validation (2): first-difference line plot. Differences are discrete ±100 jumps rather than continuous small moves.</i></p>

<p align="center">
  <img src="prosperity/assets/images/round5_ACF.png" width="75%"/>
</p>

<p align="center"><i>Validation (3): ACF plot. lag-1 is significantly negative — consistent with the negative-correlation structure produced by discretization.</i></p>

All three converged: **this is the result of projecting geometric Brownian motion onto a round-100 grid**, with theoretical justification in the literature (e.g., Roll's paper).

#### Strategy Choice

- **PEBBLE group**: pair making — post outer quotes to earn spread, take inner orders for zero-cost flattening. Prioritize shrinking risk exposure first, then per-asset position.
- **SNACK group**: chocolate/vanilla follow the PEBBLE pattern; raspberry uses EMA mean reversion; strawberry uses a long bias + mean reversion; pistachio uses a short bias + mean reversion.
- **All other assets**: treat as "GBM + no alpha" — outer quoting + zero-cost inner flattening, to avoid overfitting.

The PEBBLE pair strategy delivered **essentially zero holding PnL with linearly-growing trading PnL** — the cleanest alpha implementation of the round:

<p align="center">
  <img src="prosperity/assets/images/pebble_pnl_decompose.png" width="80%"/>
</p>

<p align="center"><i>Round 5 — PEBBLE pair-strategy PnL decomposition. Holding PnL stays in a narrow band around zero, while trading PnL accumulates steadily — the joint result of the pair structure and zero-cost inner rebalancing.</i></p>

#### Result and Biggest Retrospective: Why I Missed Alpha 3

> **This is the section of the README I most want readers to read.**

The final result was **96**.

I believe the rebound was driven by three factors:

**1.** We discovered two alphas. Mike was crucial here, particularly on the SNACK side.

**2.** We did not force-fit trend interpretations onto other assets. I still believe most assets are geometric Brownian motion; over the three days, some happened to display trend-like shapes by coincidence. Some teams may have mistaken trend for alpha — this typically leads to great backtests but poor live results.

**3.** Some teams may not have utilized all 50 assets, possibly abandoning some. They overlooked that even though single-asset volatility is much larger than the spread, dozens of Brownian motions produce variance cancellation: in trading these assets, PnL accumulates linearly while independent random risk grows with the square root.

But the reason we couldn't rebound faster is that we **missed an important alpha**:

Before the results came out, Discord discussions made me immediately realize I had **missed an alpha I could have monopolized**: **high-frequency mean reversion in round-100 jump regions**.

I had identified the structure and verified the mathematical principle through three methods — **so why didn't I trade it?** Two ingrained beliefs blinded me:

1. **"Geometric Brownian motion itself produces no alpha"** — this is correct, but **the projection onto a round-100 grid is no longer GBM; it introduces additional discretization structure**. I incorrectly extrapolated "the underlying process has no alpha" to "the projected process has no alpha".
2. **"Significantly negative ACF at lag-1 is an untradable signal because it vanishes instantly"** — this was correct in earlier rounds, where single-step reversion profit couldn't cross the spread. **But in this round, single-step jump amplitude was nearly 10× the spread** — reversion profit could comfortably cross the spread. I failed to notice that the environment parameters had changed and the old belief no longer applied.

After seeing other teams' trade-structure plots, I immediately understood the correct play: **whenever the price jumps up by 100, max-short; whenever it jumps down by 100, max-long**. This was the simplest and cleanest alpha I could have imagined for this round, and **I had all the data and validation I needed — all I lacked was a moment to step past my preexisting belief**.

> **This is the most valuable lesson from the competition for me**:
> - Research discipline ("require multiple pieces of evidence before acting") is a good habit when facing **valid signals**;
> - But the same discipline, when facing **a phenomenon whose structure has already been verified**, becomes mental inertia;
> - **When a prior belief ($A \Rightarrow \text{no alpha}$) depends on certain environment parameters (spread, jump amplitude), every round explicitly recheck whether those parameters still hold** — don't simply transfer cross-round conclusions.
>
> This also echoes the point in the [Overall Retrospective](#overall-retrospective) about "two-person discussions easily reaching consensus on mistaken beliefs" — Mike and I both agreed this was not alpha, and our consensus reinforced the error.

**Key files**:
- Strategy: [`prosperity/round5trade/version1.py`](prosperity/round5trade/version1.py)
- PEBBLE pair making: [`prosperity/round5trade/make_pebble.py`](prosperity/round5trade/make_pebble.py), [`prosperity/round5trade/PEBBLES_做市与对冲算法规范.md`](prosperity/round5trade/PEBBLES_做市与对冲算法规范.md)
- SNACK strategy: [`prosperity/round5trade/SNACK.py`](prosperity/round5trade/SNACK.py), [`prosperity/round5trade/SNACKPACK_策略规范.md`](prosperity/round5trade/SNACKPACK_策略规范.md)
- Research notebooks: [`prosperity/round5research/etf_regression.ipynb`](prosperity/round5research/etf_regression.ipynb), [`prosperity/round5research/explore_round5.ipynb`](prosperity/round5research/explore_round5.ipynb)
- Original problem statement: [`prosperity/round5research/ETF_回归分析方案.md`](prosperity/round5research/ETF_回归分析方案.md)

---

## Overall Retrospective

### Workflow Limitations

Our workflow is **"human + AI iterative discussion → human spec design → AI implementation"**. This pattern has two clear limitations:

**1. LLMs have a "common-trope tendency"**

Because LLMs are trained on the entire web, when discussing strategy directions they **always gravitate toward generic topics — microstructure imbalance signals, latency handling, queue priority** — even when I encode the competition's specific rules into `Claude.md`, the model still tends to "complicate" the problem in the direction of standard market-making/hedging, rather than searching for local alpha specific to the actual structure of the current market.

**2. Two-person discussions reinforce shared errors**

When a human and an AI iteratively converge on a consensus, if the consensus itself is wrong (e.g., in Round 5 I had a preconception that round-100 jumps had no alpha, and the AI agreed), **the discussion itself entrenches the error**. Breaking out of this requires **multiple people who can independently produce alpha engaged in adversarial discussion**. The two-person team format was a real limitation here.

### Tooling Debt

**The backtest framework was shared across all five rounds** — this saved time early on, but by Round 5 the accumulated logic branches across very different markets had bloated the framework, and the token cost of having Claude work on it kept rising. If I were to play again, I would:

- At the start of each round, write a **lightweight ad-hoc backtester** for that round's market (covering only that round's assets and mechanics);
- Have the generic backtester only carry the most stable layer — the matching mechanics.

### The Gap to Top Players

I think the gap is mainly in **exploiting deeper signals**:

- Top players, on top of the phenomena that everyone else sees, can **mine one or two non-obvious alphas** (e.g., the hard-coded extreme-deviation logic for ASH in Round 1, the round-100 jumps in Round 5). The PnL contribution of any one such alpha may not be huge, but **the gap between top players is precisely the cumulative effect of these "sub-obvious" alphas**.
- My own weakness is **the balance between research discipline and anti-inertia**: I have a decent habit of multi-evidence cross-validation (avoiding overfitting), but Round 5 exposed that my reflection frequency on my own established beliefs was insufficient — **I did not regularly ask "what environment parameters does this belief depend on, and do they still hold this round?"**.

### What I Took Away

- **Explicitly recheck cross-round assumptions every round** (especially when environment parameters — spread, volatility, tick — change in order of magnitude).
- **Separate stable core from per-round layer** in the backtest framework.
- **The value of multiple independent voices** — consensus is not necessarily truth; sometimes it's just same-direction bias accumulating.
- **Retrospectives should be written into the original research documents**, not left only as after-the-fact recollections. The `.md` spec docs in this repository are exactly the product of this habit.

---

*For any discussion or follow-up, please reach out via the contact information above.*
