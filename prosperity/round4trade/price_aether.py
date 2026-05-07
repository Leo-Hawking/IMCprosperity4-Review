"""
Aether Crystal manual trading — fair value & position sizing.

How to use
----------
1. Fill in the MARKET dict below with the bid/ask/max_volume from your screen.
2. Set BARRIER for AC_45_KO (the screen does not show it; ask the platform).
3. Run:  python price_aether.py
4. Send the printed output back.

What it does
------------
- Simulates AETHER_CRYSTAL under GBM with sigma=251%, zero drift,
  4 steps/day, 252 trading days/year. T+14 -> 10 trading days,
  T+21 -> 15 trading days.
- Prices every listed product as the mean payoff across N paths.
- Cross-checks vanillas against Black-Scholes closed form.
- Computes per-contract edge vs. bid/ask.
- Solves a small mean-variance problem (with volume caps) to suggest
  a position vector. Also reports a delta-neutral variant using the
  underlying.
"""

import numpy as np
from scipy.stats import norm
from scipy.optimize import minimize

# ---------------------------------------------------------------------------
# 1. Market input — EDIT THESE if anything on your screen differs.
# ---------------------------------------------------------------------------

S0 = 50.0  # AC mid (49.975 / 50.025)

# All option strikes/types parsed from the screenshot.
# expiry_days is in TRADING days (T+21 -> 15, T+14 -> 10, T+14/21 chooser -> 15).
# kind: 'call', 'put', 'binary_put', 'knockout_put', 'chooser'
PRODUCTS = {
    "AC":         dict(kind="underlying", bid=49.975, ask=50.025, max_vol=200),
    "AC_50_P":    dict(kind="put",  K=50, expiry_days=15, bid=12.00, ask=12.05, max_vol=50),
    "AC_50_C":    dict(kind="call", K=50, expiry_days=15, bid=12.00, ask=12.05, max_vol=50),
    "AC_35_P":    dict(kind="put",  K=35, expiry_days=15, bid=4.33,  ask=4.35,  max_vol=50),
    "AC_40_P":    dict(kind="put",  K=40, expiry_days=15, bid=6.50,  ask=6.55,  max_vol=50),
    "AC_45_P":    dict(kind="put",  K=45, expiry_days=15, bid=9.05,  ask=9.10,  max_vol=50),
    "AC_60_C":    dict(kind="call", K=60, expiry_days=15, bid=8.80,  ask=8.85,  max_vol=50),
    "AC_50_P_2":  dict(kind="put",  K=50, expiry_days=10, bid=9.70,  ask=9.75,  max_vol=50),
    "AC_50_C_2":  dict(kind="call", K=50, expiry_days=10, bid=9.70,  ask=9.75,  max_vol=50),
    "AC_50_CO":   dict(kind="chooser", K=50, choose_days=10, expiry_days=15,
                       bid=22.20, ask=22.30, max_vol=50),
    "AC_40_BP":   dict(kind="binary_put", K=40, payout=10, expiry_days=15,
                       bid=5.00, ask=5.10, max_vol=50),
    "AC_45_KO":   dict(kind="knockout_put", K=45, barrier=None,  # <-- FILL IN BARRIER
                       expiry_days=15, bid=0.150, ask=0.175, max_vol=500),
}

# If you don't know the AC_45_KO barrier yet, set this to a guess (e.g. 40)
# just to see the framework run. The KO row will be flagged as "barrier
# assumed" in the output.
DEFAULT_KO_BARRIER_GUESS = 40.0

# ---------------------------------------------------------------------------
# 2. Simulation parameters
# ---------------------------------------------------------------------------

SIGMA              = 2.51      # 251% annualized
TRADING_DAYS_YEAR  = 252
STEPS_PER_DAY      = 4
DT                 = 1.0 / (TRADING_DAYS_YEAR * STEPS_PER_DAY)
N_PATHS            = 200_000   # plenty for stable means
RNG_SEED           = 7

MAX_DAYS = 15
N_STEPS  = MAX_DAYS * STEPS_PER_DAY  # 60 steps

# ---------------------------------------------------------------------------
# 3. Path simulation (vectorized)
# ---------------------------------------------------------------------------

def simulate_paths():
    rng = np.random.default_rng(RNG_SEED)
    # log-returns per step
    drift_step = -0.5 * SIGMA ** 2 * DT
    diff_step  = SIGMA * np.sqrt(DT)
    Z = rng.standard_normal(size=(N_PATHS, N_STEPS))
    log_increments = drift_step + diff_step * Z
    log_paths = np.cumsum(log_increments, axis=1)
    # prepend t=0
    log_paths = np.concatenate([np.zeros((N_PATHS, 1)), log_paths], axis=1)
    S = S0 * np.exp(log_paths)             # shape (N_PATHS, N_STEPS+1)
    return S


def index_for_day(day):
    """Convert trading-day count to step index in the simulated path."""
    return day * STEPS_PER_DAY


# ---------------------------------------------------------------------------
# 4. Payoff functions on the simulated grid
# ---------------------------------------------------------------------------

def payoffs(S, name, p, ko_barrier_default=DEFAULT_KO_BARRIER_GUESS):
    kind = p["kind"]
    if kind == "underlying":
        # mark-to-fair = expected terminal price at the longest horizon (15d)
        # For a position held to expiry the PnL is S_T - entry. Fair entry = E[S_T].
        idx_T = index_for_day(MAX_DAYS)
        return S[:, idx_T] - S0  # PnL per share if you bought at S0

    idx_T = index_for_day(p["expiry_days"])
    S_T = S[:, idx_T]

    if kind == "call":
        return np.maximum(S_T - p["K"], 0.0)

    if kind == "put":
        return np.maximum(p["K"] - S_T, 0.0)

    if kind == "binary_put":
        return p["payout"] * (S_T < p["K"]).astype(float)

    if kind == "knockout_put":
        barrier = p.get("barrier") or ko_barrier_default
        idx_end = index_for_day(p["expiry_days"])
        path_min = S[:, : idx_end + 1].min(axis=1)
        alive = path_min > barrier        # discrete monitoring on the same grid
        return np.where(alive, np.maximum(p["K"] - S_T, 0.0), 0.0)

    if kind == "chooser":
        idx_choose = index_for_day(p["choose_days"])
        idx_end    = index_for_day(p["expiry_days"])
        S_choose   = S[:, idx_choose]
        S_T2       = S[:, idx_end]
        K          = p["K"]
        # rule from doc: at choose date pick whichever is currently ITM
        choose_call = S_choose >= K
        call_pay = np.maximum(S_T2 - K, 0.0)
        put_pay  = np.maximum(K - S_T2, 0.0)
        return np.where(choose_call, call_pay, put_pay)

    raise ValueError(kind)


# ---------------------------------------------------------------------------
# 5. Black-Scholes closed form (zero rate, zero drift)
# ---------------------------------------------------------------------------

def bs_price(kind, S, K, T, sigma):
    if T <= 0:
        if kind == "call": return max(S - K, 0.0)
        if kind == "put":  return max(K - S, 0.0)
    d1 = (np.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if kind == "call":
        return S * norm.cdf(d1) - K * norm.cdf(d2)
    if kind == "put":
        return K * norm.cdf(-d2) - S * norm.cdf(-d1)
    if kind == "binary_put":
        # cash-or-nothing put paying 1 if S_T < K
        return norm.cdf(-d2)
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main():
    print(f"Simulating {N_PATHS:,} paths, sigma={SIGMA}, dt={DT:.6f}")
    S = simulate_paths()

    # diagnostic on the underlying
    idx_15 = index_for_day(15)
    S15 = S[:, idx_15]
    print(f"  E[S_15] = {S15.mean():.4f}  (theory {S0:.4f}, zero-drift)")
    print(f"  median(S_15) = {np.median(S15):.4f}")
    print(f"  P(S_15 < 40) = {(S15 < 40).mean():.4f}")
    print(f"  P(S_15 < 45) = {(S15 < 45).mean():.4f}")
    print()

    # collect payoffs as columns -> matrix for covariance later
    names      = list(PRODUCTS.keys())
    payoff_mat = np.zeros((N_PATHS, len(names)))
    fair       = {}
    bs_check   = {}
    for j, name in enumerate(names):
        p = PRODUCTS[name]
        col = payoffs(S, name, p)
        payoff_mat[:, j] = col
        fair[name] = col.mean()

        if p["kind"] in ("call", "put", "binary_put"):
            T_years = p["expiry_days"] / TRADING_DAYS_YEAR
            theo = bs_price(p["kind"], S0, p["K"], T_years, SIGMA)
            if p["kind"] == "binary_put":
                theo *= p["payout"]
            bs_check[name] = theo

    # ---- table ----
    print(f"{'Product':<12}{'Kind':<14}{'Bid':>8}{'Ask':>8}"
          f"{'Fair(MC)':>11}{'Fair(BS)':>11}{'EdgeBuy':>9}{'EdgeSell':>9}{'MaxVol':>8}")
    print("-" * 98)
    for name in names:
        p = PRODUCTS[name]
        f = fair[name]
        bs = bs_check.get(name, np.nan)
        if p["kind"] == "underlying":
            edge_buy  = f - 0.0          # for underlying, "fair" is E[S_T]-S0; edge vs ask handled below
            edge_sell = -f
            # but more useful: just show E[S_T] - mid = ~0
        else:
            edge_buy  = f - p["ask"]      # >0 means buying is +EV
            edge_sell = p["bid"] - f      # >0 means selling is +EV

        kind_lbl = p["kind"]
        if name == "AC_45_KO" and PRODUCTS[name].get("barrier") is None:
            kind_lbl += "*"  # flag

        print(f"{name:<12}{kind_lbl:<14}"
              f"{p['bid']:>8.3f}{p['ask']:>8.3f}"
              f"{f:>11.4f}"
              f"{(bs if not np.isnan(bs) else 0):>11.4f}"
              f"{edge_buy:>9.4f}{edge_sell:>9.4f}{p['max_vol']:>8d}")
    if PRODUCTS["AC_45_KO"].get("barrier") is None:
        print(f"  * AC_45_KO priced with ASSUMED barrier = {DEFAULT_KO_BARRIER_GUESS}. "
              "Replace with the true barrier for a real number.")
    print()

    # ---- per-contract std (risk per single contract held to expiry) ----
    print("Per-contract risk (std of payoff across paths):")
    stds = payoff_mat.std(axis=0)
    for name, s in zip(names, stds):
        print(f"  {name:<12} std = {s:>8.3f}")
    print()

    # ---- naive sizing: take edge, cap at max_vol on the +EV side ----
    print("Naive sizing (ignore correlation, just take +EV side at max volume):")
    naive = {}
    naive_pnl = 0.0
    for name in names:
        if name == "AC": continue  # underlying handled in delta hedge step
        p = PRODUCTS[name]
        f = fair[name]
        if f > p["ask"]:
            qty = p["max_vol"]   # buy
            pnl = (f - p["ask"]) * qty
        elif f < p["bid"]:
            qty = -p["max_vol"]  # sell
            pnl = (p["bid"] - f) * (-qty)
        else:
            qty = 0; pnl = 0.0
        naive[name] = qty
        naive_pnl  += pnl
        if qty != 0:
            print(f"  {name:<12} qty = {qty:+5d}   expected PnL = {pnl:+8.2f}")
    print(f"  Total naive expected PnL (per contract, before x3000 multiplier): {naive_pnl:.2f}")
    print(f"  After x3000 contract size: {naive_pnl*3000:,.0f}")
    print()

    # ---- mean-variance optimization with caps ----
    # Decision variables = positions in the 11 option products + the underlying.
    # PnL per path for a position vector w (units = contracts):
    #   pnl_i = sum_j w_j * (payoff_ij - cost_j)
    # where cost_j = ask if w_j>0, bid if w_j<0. Linearize by splitting buy/sell.
    print("Mean-variance optimization (lambda controls risk aversion):")
    K = len(names)
    # For each product create two variables: long (>=0) and short (>=0).
    # net = long - short. Cost: long*ask - short*bid.
    # PnL across paths: payoff_mat @ net - (long*ask - short*bid)
    # We want to max E[PnL] - lambda * Var[PnL].

    asks = np.array([PRODUCTS[n]["ask"] for n in names])
    bids = np.array([PRODUCTS[n]["bid"] for n in names])
    caps = np.array([PRODUCTS[n]["max_vol"] for n in names], dtype=float)

    # subsample paths for the cov matrix to keep it light
    sub = payoff_mat[: 50_000]

    def neg_obj(x, lam):
        L = x[:K]; Sh = x[K:]
        net = L - Sh
        pnl_paths = sub @ net - (L @ asks - Sh @ bids)
        mu = pnl_paths.mean()
        var = pnl_paths.var()
        return -(mu - lam * var)

    bounds = [(0, c) for c in caps] + [(0, c) for c in caps]
    x0 = np.zeros(2 * K)

    for lam in [0.0, 1e-4, 1e-3, 1e-2]:
        res = minimize(neg_obj, x0, args=(lam,), method="L-BFGS-B", bounds=bounds)
        L = np.round(res.x[:K]).astype(int)
        Sh = np.round(res.x[K:]).astype(int)
        net = L - Sh
        pnl_paths = sub @ net - (L @ asks - Sh @ bids)
        print(f"\n  lambda = {lam}")
        print(f"    E[PnL]     = {pnl_paths.mean():>10.2f}    "
              f"(x3000 -> {pnl_paths.mean()*3000:>14,.0f})")
        print(f"    std(PnL)   = {pnl_paths.std():>10.2f}")
        print(f"    Sharpe     = {pnl_paths.mean()/max(pnl_paths.std(),1e-9):>10.3f}")
        print(f"    P(PnL<0)   = {(pnl_paths<0).mean():>10.3f}")
        print( "    positions:")
        for n, q in zip(names, net):
            if q != 0:
                print(f"       {n:<12} {q:+5d}")

    print()
    print("Notes:")
    print("- 'Naive sizing' assumes you can stack uncorrelated edges. You can't:")
    print("  every product is on the same underlying, so correlations are huge.")
    print("- 'lambda' = risk aversion. lambda=0 is pure expected-PnL (= naive).")
    print("  Increase it to trade Sharpe for size.")
    print("- The underlying AC is included; the optimizer will use it to delta-hedge")
    print("  if that improves the risk-adjusted PnL.")


if __name__ == "__main__":
    main()
