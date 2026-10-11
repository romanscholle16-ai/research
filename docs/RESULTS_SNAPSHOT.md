# Real-data research snapshot (cloud container, deterministic mode — no AI key configured)
Data: Binance USD-M public archive as BTCC proxy; BTCUSDT/ETHUSDT 15m/1h/4h, 2020-01-01 -> 2026-09-30, funding included. Split 50/25/25 chronological (locked test last).
Costs: unverified BTCC assumptions (docs/BTCC.md). Research loop keeps running; numbers below are a point-in-time snapshot.

```
cycles (all): 5776 | status counts: {'PROMISING': 892, 'REJECTED': 4885, 'experiments': 8478}
top rejection reasons: [('no_edge', 1745), ('statistically_insignificant', 1500), ('too_few_trades', 763), ('cost_dominated', 359), ('poor_risk_reward', 337), ('cannot_reach_target_test', 253), ('cannot_reach_target_all', 253), ('fails_multiple_testing', 244)]
why non-rejected strategies are not QUALIFIED: [('cannot_reach_target_test', 892), ('cannot_reach_target_all', 892), ('inadequate_trade_frequency', 891), ('fails_multiple_testing', 854), ('excess_drawdown_test', 61), ('excess_drawdown_all', 10)]
top by P(>= $1000/month capacity within 6 months) [month-block bootstrap, NOT a forecast]:
  P=0.035 S-9903559908 ETHUSDT 4h donchian_breakout testMeanR=1.67 n=30 adj_p=0.999 medWealth=$507 ruin=0.000
  P=0.024 S-abbcc0dd38 BTCUSDT 4h tsmom testMeanR=0.28 n=58 adj_p=1.000 medWealth=$447 ruin=0.000
  P=0.018 S-315ff626e8 BTCUSDT 4h tsmom testMeanR=0.64 n=38 adj_p=0.995 medWealth=$478 ruin=0.000
  P=0.018 S-96a714bc28 BTCUSDT 4h tsmom testMeanR=0.63 n=41 adj_p=0.998 medWealth=$475 ruin=0.000
  P=0.017 S-25210c08fa BTCUSDT 4h donchian_breakout testMeanR=0.58 n=29 adj_p=1.000 medWealth=$417 ruin=0.000
  P=0.015 S-71dfc35505 BTCUSDT 4h tsmom testMeanR=1.03 n=56 adj_p=0.987 medWealth=$477 ruin=0.000
  P=0.015 S-5a183bae59 ETHUSDT 4h donchian_breakout testMeanR=1.82 n=26 adj_p=0.998 medWealth=$477 ruin=0.000
  P=0.014 S-e6cb7da841 BTCUSDT 4h tsmom testMeanR=0.41 n=61 adj_p=0.771 medWealth=$556 ruin=0.000
```
Upgrades since first snapshot: 3 new independent families (VWAP reversion, narrow-range breakout, ETH/BTC ratio reversion), random-entry
placebo gate (beta control; existing promotions rechecked in background), diversity controls (bandit crowding penalty, per-family parent cap),
and a PORTFOLIO stage (greedy, low-correlation, concurrency-risk-scaled; selected on train+val only, judged once on the locked test).
First portfolio result (P-964a8cec3e, 8 components): 96% weekly-trade coverage on test but mean R -0.03 over 394 test trades -> REJECTED
(fails_locked_test). Selection-on-history did not generalise; this is the system working as intended.
**No strategy or portfolio is BACKTEST_QUALIFIED. The mission remains open; research continues.**
