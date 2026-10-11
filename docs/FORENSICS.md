# Forensic audit (2026-10-11 19:41 UTC)

Experiment rows: **195448** over **137897** distinct strategy specs.

## 1. How many experiments were genuinely unique?
- strategy x market rows analysed: 150381; distinct (entry logic, symbol, timeframe) hypotheses: **77486** (51.5%).
- 25466 hypotheses were tested with >1 exit variant; 72895 rows (48.5%) are exit/risk-only re-tests of an entry already tried.
- distinct hypotheses per family: gp_rule=60830, donchian_breakout=3663, taker_flow=2244, tsmom=1226, volume_absorption=1039, vol_regime_trend=1039, ict_fvg_retest=925, rsi_trend_dip=877, smc_bos_pullback=866, vol_squeeze_breakout=813, funding_contrarian=712, cross_asset_leadlag=706, vwap_reversion=462, zscore_meanrev=450

## 2. Where experiments end (stage / status)
- eligibility/fail: 102861
- screen/fail: 40596
- eligibility/pass: 33234
- screen/pass: 6925
- validate/pass: 3989
- final/fail: 3982
- validate/fail: 2931
- recheck_placebo/pass: 882
- forward/insufficient_data: 23
- portfolio_final/fail: 13
- recheck_placebo/fail: 12

## 3. Why they failed (rows can carry several reasons)
- primary class: frequency=42488 (28%), economic_infeasibility=39458 (26%), no_edge=36789 (24%), duplicate=16425 (11%), costs=14863 (10%), instability=347 (0%), other=25 (0%)
- top reasons: infeasible_for_target=42887, insufficient_frequency=32596, no_edge=24929, near_duplicate_failed_hypothesis=12298, cost_infeasible=11364, statistically_insignificant=9291, frequency_single_period=8811, cost_dominated=4394, cannot_reach_target_test=3995, cannot_reach_target_all=3995, exit_variant_limit=3979, fails_multiple_testing=3933, poor_risk_reward=3767, inadequate_trade_frequency=3615
- fee/slippage-related reason occurrences: **15947**

## 4. Rejected before any signal computation (director precheck)
- exit_variant_limit=3200694, noise_neighbourhood=567617, duplicate=1362

## 5. Does the director learn?
- candidates blocked as repeats/near-duplicates by the registry: 3769673
- director batches: 8974; candidates dropped pre-queue: 3763124
- director state: mode=None, best dev-validated t=None, experiments since improvement=0

## 6. Data availability per cell (skipped for missing data)
- none recorded

## 7. What survived
- final/fail: 3982
- recheck_placebo/fail: 12
- recheck_placebo/pass: 882
- validate/fail: 2931
- validate/pass: 3989
- locked-test failures by reason: cannot_reach_target_test=3982, cannot_reach_target_all=3982, fails_multiple_testing=3933, inadequate_trade_frequency=3615, depends_on_few_trades=1407, fails_locked_test=1373, excess_drawdown_test=1258, single_regime_dependence=589
