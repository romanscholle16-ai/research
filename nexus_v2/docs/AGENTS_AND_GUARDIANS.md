# Agents and guardians

## Ten research agents (disjoint family scopes; `tests/test_strategies_agents.py`)
| # | Agent | Families |
|---|---|---|
| 1 | Market Structure | swing_break, liq_sweep, struct_pullback |
| 2 | Momentum & Trend | donchian, ema_cross, tsmom, er_breakout |
| 3 | Mean Reversion | zscore_revert, rsi_extreme, bollinger_fade |
| 4 | Volume & Order-Flow **Proxies** | taker_imbalance, volume_spike, vwap_dev |
| 5 | BTC–ETH Relationships | ratio_z, lead_lag, corr_break (timestamp-aligned, closed bars only) |
| 6 | Volatility & Risk Regimes | vol_expansion, squeeze_break, vol_filtered_trend, highvol_revert |
| 7 | Sessions & Timing | hour_bias, session_break, dow_drift |
| 8 | Price Action | inside_bar, pin_bar, range_retest |
| 9 | Hybrid Discovery | hybrid (components from *different agents*; ablation must beat both parts; idles until ≥2 agents have supported components) |
| 10 | Adversarial | random_entry null, periodic baseline, inverse rules, randomisation challenge, abandon **recommendations** |

Each agent: own hypothesis records (hypothesis, rationale, falsifier), proposal modes (explore / refine near-misses / replicate on another
symbol-timeframe), a per-hour experiment budget (paced to 1/6 per 10 min), and persisted history. Agents *propose specs only*; evaluation,
accounting and qualification run on the shared trusted path; they cannot change thresholds, weights, the engine or the holdout.
Deterministic algorithms drive everything; the optional AI layer (`nexus/ai.py`) is advisory text with circuit-breaker fallback.

## Three guardians (separate OS processes)
* **G1 Reliability** – supervisor/child heartbeats, stale-job reclaim, quarantine notices, DB quick/full integrity, disk, log size, memory throttle,
  data freshness, trusted-config hash, 6-hourly verified checkpoints, supervisor relaunch.
* **G2 Stagnation** – 15 m…12 h activity windows (informative ≠ profitable), stuck-vs-long-running judged by heartbeat *and* progress age, family
  concentration, exhaustion, overfit families (all candidates fail walk-forward), scheduler starvation → bounded, logged, reversible weight changes
  (floor 0.05, never zero). Never touches thresholds.
* **G3 Scientific Integrity** – trusted code/config hashes, qualified-claim recomputation (revokes unsupported claims), holdout audit, counter
  reconciliation, random re-execution for reproducibility, look-ahead truncation probe, final verification before "qualified".
Guardians cannot enable orders, change goals, disable risk controls, relax thresholds or delete experiments (no such code paths exist).
