"""Fair value construction helpers for microstructure notebooks."""

from __future__ import annotations

import numpy as np
import polars as pl


def _pick_max_vol_price(row: dict, side: str, vol_threshold: float = 20.0):
    """Pick the quote with max absolute size above threshold on one side."""
    candidates: list[tuple[float, float]] = []
    for level in range(1, 4):
        p = row.get(f"{side}_price_{level}")
        v = row.get(f"{side}_volume_{level}")
        if p is None or v is None:
            continue
        v_abs = abs(float(v))
        if v_abs > vol_threshold:
            candidates.append((v_abs, float(p)))

    if not candidates:
        return None

    max_vol = max(v for v, _ in candidates)
    top_prices = [p for v, p in candidates if v == max_vol]
    if side == "bid":
        return max(top_prices)
    return min(top_prices)


def _snap_half(x: float) -> float:
    return round(x * 2.0) / 2.0


def _adhere_offset_from_norm(px: float, norm_val: float, outer_fair: float):
    """Map norm buckets to +/-2 adhesion and return inner offset."""
    if 0.5 <= norm_val <= 3.5:
        return px - 2.0 - outer_fair
    if -3.5 <= norm_val <= -0.5:
        return px + 2.0 - outer_fair
    return None


def build_outer_inner_wall_mid(
    prices_long: pl.DataFrame,
    prices_wide: pl.DataFrame,
    product: str,
    day: int | None = None,
    reg_min_volume: float = 15,
    vol_threshold: float = 20.0,
    max_stale_ms: int = 3000,
    half_spread_const: float = 10.0,
    inner_prior_offset: float = -0.5,
    inner_conflict_tol: float = 0.75,
    inner_offset_min: float = -2.0,
    inner_offset_max: float = 1.0,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Build (outer_wall_mid_df, inner_wall_mid_df, default_wall_mid_df)."""
    wide_base = prices_wide.filter(pl.col("product") == product)
    if day is not None and "day" in wide_base.columns:
        wide_base = wide_base.filter(pl.col("day") == day)

    if product == "ASH_COATED_OSMIUM":
        ash = wide_base.sort("timestamp")

        ash_rows: list[dict] = []
        last_bid = None
        last_ask = None
        last_bid_ts = None
        last_ask_ts = None
        last_outer_fair = None

        for row in ash.iter_rows(named=True):
            ts = int(row["timestamp"])
            mid_now = float(row["mid_price"]) if row.get("mid_price") is not None else None

            bid_obs = _pick_max_vol_price(row, "bid", vol_threshold=vol_threshold)
            ask_obs = _pick_max_vol_price(row, "ask", vol_threshold=vol_threshold)

            bid_cache_ok = (
                last_bid is not None
                and last_bid_ts is not None
                and (ts - last_bid_ts) <= max_stale_ms
            )
            ask_cache_ok = (
                last_ask is not None
                and last_ask_ts is not None
                and (ts - last_ask_ts) <= max_stale_ms
            )

            use_bid = bid_obs if bid_obs is not None else (last_bid if bid_cache_ok else None)
            use_ask = ask_obs if ask_obs is not None else (last_ask if ask_cache_ok else None)

            outer_source = "unavailable"
            outer_confidence = 0.0

            if use_bid is not None and use_ask is not None:
                outer_fair_px = (use_bid + use_ask) / 2.0
                outer_source = (
                    "both_sides" if (bid_obs is not None and ask_obs is not None) else "one_side_plus_cache"
                )
                outer_confidence = 1.0 if outer_source == "both_sides" else 0.8
            elif use_ask is not None:
                use_bid = use_ask - 2.0 * half_spread_const
                outer_fair_px = (use_bid + use_ask) / 2.0
                outer_source = "infer_bid_from_ask"
                outer_confidence = 0.5
            elif use_bid is not None:
                use_ask = use_bid + 2.0 * half_spread_const
                outer_fair_px = (use_bid + use_ask) / 2.0
                outer_source = "infer_ask_from_bid"
                outer_confidence = 0.5
            elif mid_now is not None:
                outer_fair_px = mid_now
                outer_source = "mid_fallback"
                outer_confidence = 0.4
            elif last_outer_fair is not None:
                outer_fair_px = last_outer_fair
                outer_source = "carry_fair"
                outer_confidence = 0.25
            else:
                outer_fair_px = None

            inner_fair_px = None
            inner_offset = None
            inner_source = "inner_unavailable"
            inner_confidence = 0.0

            if outer_fair_px is not None:
                baseline_offset = inner_prior_offset
                baseline_inner_fair = outer_fair_px + baseline_offset

                bid1 = float(row["bid_price_1"]) if row.get("bid_price_1") is not None else None
                ask1 = float(row["ask_price_1"]) if row.get("ask_price_1") is not None else None
                bid1_vol = abs(float(row["bid_volume_1"])) if row.get("bid_volume_1") is not None else 1.0
                ask1_vol = abs(float(row["ask_volume_1"])) if row.get("ask_volume_1") is not None else 1.0

                offset_candidates: list[float] = []
                offset_weights: list[float] = []

                if bid1 is not None:
                    bid1_norm = bid1 - baseline_inner_fair
                    bid_off = _adhere_offset_from_norm(bid1, bid1_norm, outer_fair_px)
                    if bid_off is not None:
                        offset_candidates.append(bid_off)
                        offset_weights.append(max(1.0, bid1_vol))

                if ask1 is not None:
                    ask1_norm = ask1 - baseline_inner_fair
                    ask_off = _adhere_offset_from_norm(ask1, ask1_norm, outer_fair_px)
                    if ask_off is not None:
                        offset_candidates.append(ask_off)
                        offset_weights.append(max(1.0, ask1_vol))

                if len(offset_candidates) >= 2 and abs(offset_candidates[0] - offset_candidates[1]) > inner_conflict_tol:
                    target_offset = baseline_offset
                    inner_source = "inner_conflict_fallback"
                    inner_confidence = 0.35
                elif len(offset_candidates) > 0:
                    wsum = sum(offset_weights)
                    target_offset = sum(v * w for v, w in zip(offset_candidates, offset_weights)) / wsum
                    inner_source = "inner_adhered_to_pm2"
                    inner_confidence = 0.85
                else:
                    target_offset = baseline_offset
                    inner_source = "inner_no_adhere_signal"
                    inner_confidence = 0.55

                bounded_offset = min(max(target_offset, inner_offset_min), inner_offset_max)
                inner_offset = _snap_half(bounded_offset)
                inner_fair_px = outer_fair_px + inner_offset

            if bid_obs is not None:
                last_bid = bid_obs
                last_bid_ts = ts
            if ask_obs is not None:
                last_ask = ask_obs
                last_ask_ts = ts
            if outer_fair_px is not None:
                last_outer_fair = outer_fair_px

            ash_rows.append(
                {
                    "day": row.get("day"),
                    "timestamp": ts,
                    "product": row["product"],
                    "raw_mid": mid_now,
                    "outer_wall_mid": outer_fair_px,
                    "outer_source": outer_source,
                    "outer_confidence": outer_confidence,
                    "inner_wall_mid": inner_fair_px,
                    "inner_offset": inner_offset,
                    "inner_source": inner_source,
                    "inner_confidence": inner_confidence,
                    "bid_obs": bid_obs,
                    "ask_obs": ask_obs,
                    "bid_used": use_bid,
                    "ask_used": use_ask,
                    "half_spread_est": half_spread_const,
                }
            )

        fair_dual_df = pl.DataFrame(ash_rows).sort("timestamp")

        outer_wall_mid_df = fair_dual_df.select(
            [
                "day",
                "timestamp",
                "product",
                "raw_mid",
                pl.col("outer_wall_mid").alias("wall_mid"),
                pl.col("outer_source").alias("fair_source"),
                pl.col("outer_confidence").alias("fair_confidence"),
                "half_spread_est",
            ]
        )

        inner_wall_mid_df = fair_dual_df.select(
            [
                "day",
                "timestamp",
                "product",
                "raw_mid",
                pl.col("inner_wall_mid").alias("wall_mid"),
                pl.col("inner_source").alias("fair_source"),
                pl.col("inner_confidence").alias("fair_confidence"),
                "inner_offset",
            ]
        )

        wall_mid_df = outer_wall_mid_df
    else:
        reg_src = prices_long.filter((pl.col("product") == product) & (pl.col("volume") > reg_min_volume))
        if day is not None and "day" in reg_src.columns:
            reg_src = reg_src.filter(pl.col("day") == day)
        reg_src = reg_src.group_by("timestamp").agg(pl.col("price").mean().alias("reg_price")).sort("timestamp")

        if reg_src.height >= 2:
            x = reg_src["timestamp"].to_numpy()
            y = reg_src["reg_price"].to_numpy()
            k, b = np.polyfit(x, y, 1)
        else:
            k = 0.0
            mid_vals = (
                wide_base["mid_price"].drop_nulls()
                if "mid_price" in wide_base.columns
                else pl.Series([], dtype=pl.Float64)
            )
            b = float(mid_vals.mean()) if len(mid_vals) else 0.0

        outer_wall_mid_df = (
            wide_base.select(["day", "timestamp", "product", "mid_price"])
            .with_columns(
                [
                    pl.col("mid_price").alias("raw_mid"),
                    (k * pl.col("timestamp") + b).round(0).alias("wall_mid"),
                    pl.lit("regression").alias("fair_source"),
                    pl.lit(0.6).alias("fair_confidence"),
                    pl.lit(half_spread_const).alias("half_spread_est"),
                ]
            )
            .drop("mid_price")
        )

        inner_wall_mid_df = outer_wall_mid_df.with_columns(
            [
                (pl.col("wall_mid") + inner_prior_offset).alias("wall_mid"),
                pl.lit("outer_minus_0_5").alias("fair_source"),
                pl.lit(0.5).alias("fair_confidence"),
                pl.lit(inner_prior_offset).alias("inner_offset"),
            ]
        )

        wall_mid_df = outer_wall_mid_df

    return outer_wall_mid_df, inner_wall_mid_df, wall_mid_df
