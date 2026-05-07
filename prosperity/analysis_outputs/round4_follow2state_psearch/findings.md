# Round 4 follow_2state hyperparameter search

Run A (full grid)
- Round 4 days 0, 1, 2
- Grid size: 960
- Objective: total PnL (sum across products) with plateau score = min neighbor PnL (1-step)

Best point and plateau (Run A)
- Peak PnL: 324983 at weak_frac=0.8, strong_mult_near=0.9, strong_mult_taker=1.3, entry_scale=1.0, follow_max_frac=0.3
- Plateau best: same point, plateau_score=321699

Run B (reduced grid)
- Grid size: 48
- Search ranges: weak_frac {0.7, 0.8}, strong_mult_near {0.9, 1.0, 1.1}, strong_mult_taker {1.3, 1.5}, entry_scale {0.85, 1.0}, follow_max_frac {0.3, 0.5}

Best point and plateau (Run B)
- Peak PnL: 324983 at weak_frac=0.8, strong_mult_near=0.9, strong_mult_taker=1.3, entry_scale=1.0, follow_max_frac=0.3
- Plateau best: same point, plateau_score=321699

Stability notes (from Run B)
- follow_max_frac still flat at the top: 0.3 and 0.5 both hit the same peak and plateau.
- strong_mult_taker=1.3 beats 1.5 on average.
- weak_frac=0.8 beats 0.7 on average.
- entry_scale=1.0 beats 0.85 on average.
- strong_mult_near 0.9 is best; 1.1 close, 1.0 slightly worse.

Suggested next slice (if refining)
- Keep weak_frac=0.8, strong_mult_taker=1.3, entry_scale=1.0
- Scan strong_mult_near in a tighter band: 0.85, 0.9, 0.95, 1.0, 1.05, 1.1
- follow_max_frac looks insensitive here; keep 0.4 or 0.5 unless a follow-trigger diagnostic suggests otherwise
