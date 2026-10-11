# Research efficiency report
_generated 2026-10-11 08:43 UTC; read-only audit of the research DB (history preserved)_

## Distinctness
- strategy×market tests: **41,944**; genuinely distinct hypotheses (entry definition × market × timeframe × side): **9,441**; exit-only duplicates: **32,503**; distinct entry definitions across markets: 4,238
- hypotheses with an adjacent-grid-step neighbour (minor parameter variants): **7,042**; most exit variants of a single hypothesis: [78, 77, 76, 76, 73]

## Compute by family (share of total experiment time)
| family | tests | share | secs | screen pass | validate pass | promising | freq-fail | cost/econ-fail | no-edge |
|---|---|---|---|---|---|---|---|---|---|
| funding_contrarian | 2,232 | 22.8% | 2,957 | 1190 | 189 | 15 | 175 | 630 | 877 |
| vol_squeeze_breakout | 2,042 | 20.6% | 2,668 | 609 | 16 | 0 | 8 | 1066 | 553 |
| ict_fvg_retest | 2,013 | 19.0% | 2,459 | 683 | 5 | 0 | 21 | 726 | 635 |
| vol_regime_trend | 2,289 | 17.4% | 2,258 | 1347 | 251 | 6 | 171 | 642 | 1086 |
| donchian_breakout | 6,801 | 6.3% | 823 | 3430 | 1128 | 260 | 491 | 1916 | 2229 |
| tsmom | 3,309 | 5.2% | 680 | 2901 | 1301 | 894 | 538 | 344 | 1295 |
| taker_flow | 2,349 | 1.4% | 181 | 1382 | 232 | 101 | 810 | 668 | 628 |
| session_drift | 2,070 | 1.3% | 164 | 1164 | 281 | 41 | 692 | 394 | 547 |
| smc_bos_pullback | 2,028 | 1.2% | 155 | 624 | 0 | 0 | 2 | 865 | 609 |
| vwap_reversion | 1,952 | 0.8% | 99 | 514 | 0 | 0 | 54 | 594 | 459 |
| failed_breakout_fade | 1,900 | 0.7% | 86 | 444 | 30 | 14 | 21 | 538 | 406 |
| liquidity_sweep_reversal | 1,918 | 0.6% | 74 | 499 | 1 | 0 | 2 | 580 | 492 |
| eth_btc_ratio_reversion | 1,920 | 0.6% | 73 | 494 | 1 | 0 | 24 | 569 | 457 |
| zscore_meanrev | 1,965 | 0.5% | 70 | 522 | 0 | 0 | 12 | 560 | 508 |
| rsi_trend_dip | 2,012 | 0.5% | 69 | 555 | 0 | 0 | 522 | 569 | 409 |
| narrow_range_breakout | 1,574 | 0.5% | 64 | 487 | 14 | 4 | 1 | 425 | 468 |
| cross_asset_leadlag | 1,948 | 0.4% | 56 | 510 | 6 | 1 | 62 | 960 | 412 |
| volume_absorption | 1,622 | 0.3% | 37 | 391 | 0 | 0 | 1300 | 222 | 42 |

Families with no validation-stage survivors: smc_bos_pullback, zscore_meanrev, volume_absorption, rsi_trend_dip, vwap_reversion

## Compute by timeframe
| tf | tests | share | secs | validate pass | promising | freq-fail | cost/econ-fail |
|---|---|---|---|---|---|---|---|
| 5m | 24,053 | 77.4% | 10,048 | 0 | 0 | 1860 | 9808 |
| 1h | 7,478 | 10.5% | 1,367 | 926 | 71 | 577 | 1906 |
| 4h | 8,473 | 9.3% | 1,201 | 2510 | 1265 | 2316 | 287 |
| 15m | 1,940 | 2.8% | 357 | 19 | 0 | 153 | 267 |

Timeframes using >5% of compute with zero promising candidates: 5m

## Why strategies fail
| primary class | count |
|---|---|
| no_edge | 12,112 |
| duplicate | 11,195 |
| costs | 10,430 |
| frequency | 4,906 |
| economic_infeasibility | 1,838 |
| promising | 1,336 |
| instability | 127 |

Top individual reasons (a strategy can carry several): near_duplicate_failed_hypothesis 11,195, cost_infeasible 10,271, no_edge 7,816, statistically_insignificant 3,510, cannot_reach_target_test 2,541, cannot_reach_target_all 2,541, fails_multiple_testing 2,492, inadequate_trade_frequency 2,355, too_few_trades 1,982, insufficient_frequency 1,814, infeasible_for_target 1,712, cost_dominated 983

## Is the early filter running before expensive work?
- rows with an eligibility stage: 27,658 (legacy rows without: 14,286); rejected at eligibility without any backtest: 24,198
- rows that ran a backtest AFTER failing eligibility: **0** (must be 0)
- compute wasted on frequency/cost/infeasible/duplicate outcomes: legacy 1,203s of 2,892s (**41.6%**) vs current 8,618s of 10,082s (**85.5%**)
- average seconds per test: legacy 0.202s, current 0.365s

## Are the PROMISING candidates still viable under current rules?
- checked 1,336 PROMISING candidates (from stored trades); **10** pass the current frequency + economic screen. Failures: {'insufficient_frequency': 1254, 'infeasible_for_target': 1326, 'weekly_coverage_below_gate': 1320}
- by timeframe: {'4h:survivor_only': 1265, '1h:survivor_only': 61, '1h:viable': 10}
- target arithmetic: +61%/month sustained; round-trip cost 0.1830% of notional (conservative BTCC assumption, unverified)
