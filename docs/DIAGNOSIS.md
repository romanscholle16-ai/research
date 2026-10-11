# Diagnosis — why strategies are rejected (generated 2026-10-11 12:10 UTC, last 4 days)

## 1. Funnel (non-4h, real data)

| stage | pass | fail |
|---|---|---|
| eligibility | 9848 | 32810 |
| screen | 1103 | 14559 |
| validate | 543 | 558 |
| final | 0 | 543 |

**eligibility failures:** near_duplicate_failed_hypothesis 11899, cost_infeasible 10854, infeasible_for_target 5651, insufficient_frequency 4599, frequency_single_period 764, exit_variant_limit 601, noise_neighbourhood 63

**screen failures:** no_edge 11380, statistically_insignificant 1715, cost_dominated 1006, poor_risk_reward 970, too_few_trades 458

**validate failures:** fails_out_of_sample 524, fails_walk_forward 21, unstable_parameters_overfit 16, regime_dependent_or_unstable 3

**final failures:** cannot_reach_target_test 543, cannot_reach_target_all 543, fails_multiple_testing 543, excess_drawdown_test 376, inadequate_trade_frequency 333, depends_on_few_trades 268, fails_locked_test 259

## 2. Gross edge vs cost per timeframe (screen stage, train data)

| tf | screens | median gross R/trade | median net R/trade | median cost (R) | gross>0 | net>0 | net>0.05R | t>2 | trades/yr |
|---|---|---|---|---|---|---|---|---|---|
| 5m | 3235 | -0.117 | -0.340 | 0.163 | 3% | 0% | 0.4% | 0.0% | 1522 |
| 15m | 4951 | -0.028 | -0.106 | 0.070 | 31% | 11% | 4.5% | 0.5% | 373 |
| 1h | 4893 | 0.067 | 0.008 | 0.057 | 69% | 52% | 40.2% | 8.6% | 117 |

## 3. Candidates that reach the locked-test stage (non-4h)

- 543 candidates; median locked-test mean R/trade **0.006**, positive in 52%, t>2 in 0.0%; median trades/month 7.3
- arithmetic: at 7.3 trades/month and 0.006 R/trade, risking 5% per trade yields ≈ 0.2% per month; the target needs ≈ 61% per month.

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
