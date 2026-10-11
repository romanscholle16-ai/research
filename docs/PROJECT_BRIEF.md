# StrategyLab: full project brief and handoff prompt

## Handoff prompt (paste into a new Claude Code session on this repo, branch `claude/eager-euler-tsfk7q`)

You are continuing StrategyLab, an autonomous crypto-futures strategy research system in this repository. Do not rebuild it. Read `README.md` and `docs/` first.

**Mission (unchanged):** find a strategy or portfolio that, on BTCUSDT/ETHUSDT perpetuals with realistic BTCC costs, turns $300 into a state where at least $1,000/month of realized net profit can be withdrawn within 6 months (stretch $2,000-$3,000), keeps at least 50% of every profitable month in the account, trades at least once every calendar week, and survives rigorous validation. Success needs every gate passed and the result reproducible. Never fabricate or relax gates to get a win. Live trading stays disabled.

**Operating rules:** research never stops because strategies fail; log every experiment; learn from failures; explore materially different ideas; AI proposes, deterministic code decides; no look-ahead; the locked test slice is used once per candidate; flag every unverifiable assumption (BTCC fees, tiers, margin, funding are UNVERIFIED).

**State at handoff:** about 15,400 experiments over 8,858 distinct strategies on real 2020-2026 data. 0 BACKTEST_QUALIFIED. About 1,400 PROMISING, all failing the weekly-trade gate, the $1k/month gate and multiple-testing. Best simulated chance of $1k/month within 6 months is about 4%. Two 8-strategy portfolios were rejected on the locked test.

**Next work, in priority order:**
1. Verify BTCC fees, leverage tiers, maintenance margin and minimum sizes from authoritative sources and update `strategylab/btcc.py`; check jurisdiction/product eligibility.
2. Set `ANTHROPIC_API_KEY` so Claude-generated hypotheses run (only a mock provider has been exercised) and review what it proposes.
3. Frequency: nearly all promising rules are 4h, long-biased, trade rarely. Search for genuinely independent higher-frequency edges (1m/5m data, order-flow, funding/OI/liquidation data if obtainable), and event-driven and cross-asset ideas.
4. Calibrate the pipeline on null data (shuffled/block-bootstrapped returns) to measure the false-PROMISING rate; tighten gates if it is high.
5. Replace the Sidak multiple-testing correction with a less conservative but valid method (FDR / deflated Sharpe) and document the choice.
6. Paper-forward validation module (new months of data) before any strategy can be PAPER_VALIDATED.
7. Long soak test on the Windows target machine; Windows autostart script is untested.

## What the system is
Persistent Python service (`strategylab/service.py` supervises `worker.py`) with a SQLite research memory. Jobs: `plan`, `experiment`, `portfolio`, `recheck`. Watchdog kills stalled or over-time workers, restarts with backoff, requeues interrupted jobs, restarts on memory-limit exit, and resumes after a hard kill. Dashboard (`dashboard.py`) offers Start/Pause/Resume/Stop, a leaderboard and a $300 capital fan chart.

Agents: Research Scientist (hypothesis catalog, optional AI proposals), Strategy Inventor (concrete parameters), Evolution Specialist (mutations of near-misses, capped per family), Supervisor (bandit explore/exploit with a crowding penalty and dedupe), plus Backtesting, Validation and Risk modules.

## How strategies are tested
**Data:** Binance USD-M public archive (BTCC has no reachable history), 1-minute klines with taker-buy volume and funding resampled to 15m/1h/4h, 2020-01-01 to 2026-09-30, no gaps. No open interest, liquidations, order book or bid/ask. Split chronologically: train 50%, validation 25%, locked test 25%.

**Backtest rules:** signals use closed bars only and execute at the next open. Taker fees 0.06% a side, spread and slippage added, stop-market slippage, stop assumed first when stop and target share a bar, gap-through stops fill at the open, funding charged at 00/08/16 UTC, one position at a time.

**Pipeline per strategy x market x timeframe:**
1. Screen on train: enough trades, positive net expectancy in R, t-stat, profit factor. Reasons recorded (no edge, cost dominated, too few trades...).
2. Validate on validation slice: positive out-of-sample, 6-fold consistency, anchored walk-forward optimization, parameter-neighbour sensitivity, 1.5x/2x cost stress, look-ahead test (signals recomputed on truncated data must match), random-entry placebo test (beta control).
3. Final: risk, leverage and margin mode fitted on train+validation only, then one-shot locked test, weekly trade coverage, $300 Monte Carlo (month-block bootstrap plus real rolling 6-month windows) with liquidation, min lot sizes and 50% profit retention, then Sidak multiple-testing across every strategy tried.
4. Portfolio stage: low-correlation greedy selection using train+validation only, risk scaled for overlapping positions, judged once on the locked test.

**Statuses:** REJECTED, PROMISING (positive after validation and the locked test but fails the objective), BACKTEST_QUALIFIED (all gates). PAPER_VALIDATED and LIVE_ELIGIBLE are never assigned automatically.

## What has been tested (real data, snapshot)
17 strategy families: tsmom 2,396, donchian breakout 1,620, session drift 903, vol-regime trend 856, funding contrarian 814, taker-flow 766, vol-squeeze breakout 318, narrow-range breakout 296, ICT fair-value-gap retest 290, cross-asset lead-lag 278, ETH/BTC ratio reversion 265, liquidity-sweep reversal 262, z-score mean reversion 260, SMC break-of-structure pullback 260, VWAP reversion 258, volume absorption 258, RSI trend-dip 256. By market and timeframe, tested / promising: BTC 15m 647/0, BTC 1h 1,088/27, BTC 4h 3,914/1,024, ETH 15m 676/0, ETH 1h 1,428/96, ETH 4h 2,603/251.

Stage funnel: 10,357 reached screening, 2,315 passed, 1,812 passed validation, about 1,400 survived the locked test as PROMISING, 0 qualified. Top rejection reasons: no edge 3,569; statistically insignificant 2,456; too few trades 1,415; cost dominated 602; poor risk/reward 562; fails out of sample 387; fails locked test 333; unstable parameters 62. Among PROMISING, the blockers are: cannot reach $1k/month (402), fails multiple testing (393), inadequate trade frequency (388), excess drawdown (152).

Findings: only trend/breakout/session-drift style rules on 4h (and some 1h) survive; every 15m rule and all mean-reversion, SMC/ICT, VWAP, absorption and lead-lag families produced zero promising results after costs. 882 of 894 rechecked promising rules beat random entries (p near 0.01), so their edge is not just market beta, but this is before correcting for the number tried. Two portfolios (8 components each) reached about 96% weekly coverage on train+validation and then lost on the locked test.

## Caveats
The locked test has been seen by every strategy that reached the final stage, so single-strategy test results are optimistic; portfolios were selected without it. Monte Carlo output is a statistical estimate, not a forecast. BTCC costs are unverified assumptions. The 100-cycle autonomy test used synthetic data and shows engine behaviour, not alpha or 24/7 stability. The research service only runs while the host machine or container is up.
