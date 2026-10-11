# Real-data research snapshot (cloud container, deterministic mode — no AI key was configured)
Data: Binance USD-M public archive as BTCC proxy; BTCUSDT/ETHUSDT 15m/1h/4h, 2020-01-01 → 2026-09-30 (1m source resampled; 0 gaps), funding included.
Split: train 50 % / validation 25 % / locked test 25 % (chronological). Costs: unverified BTCC assumptions (docs/BTCC.md).
**Result: no strategy is BACKTEST_QUALIFIED.** The research loop was still running when this snapshot was taken; the numbers keep growing.

```
cycles (all): 1520 | status counts: {'PROMISING': 148, 'REJECTED': 1372, 'experiments': 2010}
top rejection reasons: [('no_edge', 594), ('statistically_insignificant', 370), ('too_few_trades', 188), ('cost_dominated', 98), ('poor_risk_reward', 91), ('cannot_reach_target_test', 72), ('cannot_reach_target_all', 72), ('fails_multiple_testing', 72)]
why non-rejected strategies are not QUALIFIED: [('inadequate_trade_frequency', 148), ('cannot_reach_target_test', 148), ('cannot_reach_target_all', 148), ('fails_multiple_testing', 141), ('excess_drawdown_test', 21), ('excess_drawdown_all', 4)]
top by P(>= $1000/month capacity within 6 months) [month-block bootstrap, NOT a forecast]:
  P=0.035 S-9903559908 ETHUSDT 4h donchian_breakout testMeanR=1.67 n=30 adj_p=0.999 medWealth=$507 ruin=0.000
  P=0.018 S-315ff626e8 BTCUSDT 4h tsmom testMeanR=0.64 n=38 adj_p=0.995 medWealth=$478 ruin=0.000
  P=0.018 S-96a714bc28 BTCUSDT 4h tsmom testMeanR=0.63 n=41 adj_p=0.998 medWealth=$475 ruin=0.000
  P=0.015 S-71dfc35505 BTCUSDT 4h tsmom testMeanR=1.03 n=56 adj_p=0.987 medWealth=$477 ruin=0.000
  P=0.015 S-5a183bae59 ETHUSDT 4h donchian_breakout testMeanR=1.82 n=26 adj_p=0.998 medWealth=$477 ruin=0.000
  P=0.014 S-e6cb7da841 BTCUSDT 4h tsmom testMeanR=0.41 n=61 adj_p=0.771 medWealth=$556 ruin=0.000
  P=0.013 S-c07ebb06f3 BTCUSDT 4h session_drift testMeanR=0.37 n=70 adj_p=1.000 medWealth=$421 ruin=0.000
  P=0.013 S-5dff64e029 BTCUSDT 4h tsmom testMeanR=0.37 n=71 adj_p=0.967 medWealth=$494 ruin=0.000
```
Reading it honestly:
* The "PROMISING" strategies are mostly **long-only 4h trend/breakout/session-drift** rules: they are positive on the locked 2025–26 test slice but
  they are largely crypto beta in a trending sample, trade too rarely (every one fails the ≥1-trade-per-week gate), fail the Šidák multiple-testing
  gate (adjusted p ≈ 1), and best-case simulate only a few % chance of ≥ $1,000/month withdrawal capacity within six months from $300.
* That "PROMISING" is a *deliberately low* label ("positive, survived validation and the one-shot test") — it is not evidence of a tradeable edge.
* The $300 → $1,000+/month target remains unachieved in this snapshot; research continues by design.
