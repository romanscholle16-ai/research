# Research efficiency report
_generated 2026-10-11 09:01 UTC; read-only audit of the research DB (history preserved)_

## Distinctness
- strategy×market tests: **44,784**; genuinely distinct hypotheses (entry definition × market × timeframe × side): **10,260**; exit-only duplicates: **34,524**; distinct entry definitions across markets: 4,618
- hypotheses with an adjacent-grid-step neighbour (minor parameter variants): **7,534**; most exit variants of a single hypothesis: [79, 77, 77, 76, 76]

## Compute by family (share of total experiment time)
| family | tests | share | secs | screen pass | validate pass | promising | freq-fail | cost/econ-fail | no-edge |
|---|---|---|---|---|---|---|---|---|---|
| funding_contrarian | 2,335 | 22.9% | 3,100 | 1218 | 190 | 15 | 175 | 674 | 902 |
| vol_squeeze_breakout | 2,144 | 20.6% | 2,788 | 631 | 16 | 0 | 8 | 1131 | 573 |
| ict_fvg_retest | 2,160 | 19.0% | 2,576 | 728 | 5 | 0 | 21 | 783 | 676 |
| vol_regime_trend | 2,430 | 17.6% | 2,392 | 1401 | 252 | 6 | 171 | 695 | 1136 |
| donchian_breakout | 7,034 | 6.1% | 827 | 3473 | 1132 | 260 | 502 | 2002 | 2263 |
| tsmom | 3,446 | 5.0% | 684 | 2961 | 1305 | 894 | 540 | 388 | 1354 |
| taker_flow | 2,564 | 1.4% | 188 | 1430 | 235 | 101 | 899 | 739 | 667 |
| session_drift | 2,156 | 1.2% | 165 | 1185 | 283 | 41 | 718 | 415 | 565 |
| smc_bos_pullback | 2,162 | 1.2% | 162 | 661 | 0 | 0 | 2 | 929 | 645 |
| vwap_reversion | 2,112 | 0.8% | 104 | 575 | 0 | 0 | 70 | 631 | 520 |
| failed_breakout_fade | 2,096 | 0.7% | 91 | 510 | 30 | 14 | 36 | 611 | 468 |
| liquidity_sweep_reversal | 2,050 | 0.6% | 77 | 535 | 1 | 0 | 5 | 631 | 528 |
| eth_btc_ratio_reversion | 2,049 | 0.6% | 77 | 528 | 1 | 0 | 24 | 620 | 489 |
| zscore_meanrev | 2,112 | 0.5% | 74 | 574 | 0 | 0 | 13 | 608 | 560 |
| rsi_trend_dip | 2,151 | 0.5% | 72 | 587 | 0 | 0 | 566 | 602 | 441 |
| narrow_range_breakout | 1,607 | 0.5% | 65 | 498 | 14 | 4 | 1 | 433 | 479 |
| cross_asset_leadlag | 2,102 | 0.4% | 59 | 548 | 6 | 1 | 69 | 1038 | 448 |
| volume_absorption | 1,801 | 0.3% | 41 | 392 | 0 | 0 | 1459 | 232 | 43 |
| gp_rule | 273 | 0.1% | 17 | 129 | 9 | 0 | 80 | 78 | 113 |

Families with no validation-stage survivors: smc_bos_pullback, zscore_meanrev, volume_absorption, rsi_trend_dip, vwap_reversion

## Compute by timeframe
| tf | tests | share | secs | validate pass | promising | freq-fail | cost/econ-fail |
|---|---|---|---|---|---|---|---|
| 5m | 25,178 | 78.1% | 10,592 | 1 | 0 | 1958 | 10287 |
| 1h | 8,523 | 10.2% | 1,379 | 944 | 71 | 822 | 2305 |
| 4h | 8,473 | 8.9% | 1,201 | 2510 | 1265 | 2316 | 287 |
| 15m | 2,610 | 2.9% | 387 | 24 | 0 | 263 | 361 |

Timeframes using >5% of compute with zero promising candidates: 5m

## Why strategies fail
| primary class | count |
|---|---|
| no_edge | 12,870 |
| duplicate | 11,851 |
| costs | 10,955 |
| frequency | 5,359 |
| economic_infeasibility | 2,285 |
| promising | 1,336 |
| instability | 128 |

Top individual reasons (a strategy can carry several): near_duplicate_failed_hypothesis 11,784, cost_infeasible 10,782, no_edge 8,484, statistically_insignificant 3,578, cannot_reach_target_test 2,542, cannot_reach_target_all 2,542, fails_multiple_testing 2,493, inadequate_trade_frequency 2,356, insufficient_frequency 2,257, infeasible_for_target 2,164, too_few_trades 1,982, cost_dominated 1,041

## Is the early filter running before expensive work?
- rows with an eligibility stage: 30,498 (legacy rows without: 14,286); rejected at eligibility without any backtest: 26,220
- rows that ran a backtest AFTER failing eligibility: **0** (must be 0)
- compute wasted on frequency/cost/infeasible/duplicate outcomes: legacy 1,203s of 2,892s (**41.6%**) vs current 9,163s of 10,668s (**85.9%**)
- average seconds per test: legacy 0.202s, current 0.35s

## Are the PROMISING candidates still viable under current rules?
- checked 1,336 PROMISING candidates (from stored trades); **10** pass the current frequency + economic screen. Failures: {'insufficient_frequency': 1254, 'infeasible_for_target': 1326, 'weekly_coverage_below_gate': 1320}
- by timeframe: {'4h:survivor_only': 1265, '1h:survivor_only': 61, '1h:viable': 10}
- target arithmetic: +61%/month sustained; round-trip cost 0.1830% of notional (conservative BTCC assumption, unverified)
