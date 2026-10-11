# Diagnosis — why strategies are rejected (generated 2026-10-11 09:38 UTC, last 4 days)

## 1. Funnel (non-4h, real data)

| stage | pass | fail |
|---|---|---|
| eligibility | 5020 | 27302 |
| screen | 984 | 9850 |
| validate | 487 | 496 |
| final | 0 | 487 |

**eligibility failures:** near_duplicate_failed_hypothesis 11786, cost_infeasible 10821, insufficient_frequency 2680, infeasible_for_target 2650, frequency_single_period 580, exit_variant_limit 196, noise_neighbourhood 24

**screen failures:** no_edge 7306, statistically_insignificant 1275, cost_dominated 811, poor_risk_reward 807, too_few_trades 458

**validate failures:** fails_out_of_sample 472, fails_walk_forward 15, unstable_parameters_overfit 13, regime_dependent_or_unstable 1

**final failures:** cannot_reach_target_test 487, cannot_reach_target_all 487, fails_multiple_testing 487, excess_drawdown_test 339, inadequate_trade_frequency 304, fails_locked_test 222, depends_on_few_trades 220

## 2. Gross edge vs cost per timeframe (screen stage, train data)

| tf | screens | median gross R/trade | median net R/trade | median cost (R) | gross>0 | net>0 | net>0.05R | t>2 | trades/yr |
|---|---|---|---|---|---|---|---|---|---|
| 5m | 3224 | -0.117 | -0.340 | 0.163 | 3% | 0% | 0.4% | 0.0% | 1525 |
| 15m | 2816 | -0.048 | -0.167 | 0.104 | 28% | 9% | 4.9% | 0.6% | 446 |
| 1h | 4624 | 0.075 | 0.012 | 0.059 | 69% | 53% | 41.7% | 9.1% | 116 |

## 3. Candidates that reach the locked-test stage (non-4h)

- 487 candidates; median locked-test mean R/trade **0.015**, positive in 54%, t>2 in 0.0%; median trades/month 7.3
- median simulated $300 → month-6 wealth: **213%** of start (target needs ≥ +$1,000/month withdrawable)
- arithmetic: at 7.3 trades/month and 0.015 R/trade, risking 5% per trade yields ≈ 0.5% per month; the target needs ≈ 61% per month.

## 4. Root causes (ranked)
1. **No gross edge on short timeframes.** Median gross R/trade on 5m/15m is negative; the entry ideas (price/volume/flow/funding rules from public data) are not predictive at that horizon, and round-trip cost (0.10–0.16 R per trade) makes it worse. Searching more of the same on 5m is wasted compute — the director now suppresses cells whose recent screens show no gross edge.
2. **1h has a small positive edge (≈0.02–0.08 R/trade, mostly trend/drift) — far too small for the target.** Roughly 100 trades/yr × ~0.05 R × 5% risk ≈ 3% of equity per year before leverage; the target is +61%/month. The gap is more than an order of magnitude, and scaling by leverage multiplies drawdown/ruin first.
3. **Almost nothing survives out-of-sample.** Walk-forward/OOS failures and multiple-testing (Sidak over tens of thousands of trials) eliminate the apparent 1h winners; no candidate has t>2 on the locked test.
4. **Sample is two assets.** BTC and ETH are highly correlated, so the effective number of independent trade opportunities is small. Breadth (more perpetual markets) is the one lever that raises both trade frequency and diversification without raising per-trade risk.
5. **Duplicate/overfit churn** (already fixed in Phase 3b): exit-only variants and neighbouring parameters are now rejected before backtesting.

## 5. What works / what does not (for reference)
- WORKS: pipeline integrity (null-calibration, look-ahead test, placebo), honest cost model, crash-safe multi-worker service, pre-test filters, director + registry, GP grammar.
- DOES NOT: any 5m/15m rule family tried so far (negative gross edge); parameter-tuned 1h trend rules (decay OOS); portfolios of non-qualifying components (no diversification benefit large enough).
- UNPROVEN: that any public-data rule reaches +61%/month honestly. The system keeps searching (new markets, regime-conditional rules, GP) without relaxing gates.
