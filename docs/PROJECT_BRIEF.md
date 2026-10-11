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


---
## Phase 2 update (2026-10-11): what changed since the first brief
Verified at the start of phase 2 against the live database: 9,817 strategies / 17,044 experiment rows, 0 BACKTEST_QUALIFIED, one service + one worker (no duplicates). Checkpoint tag: `checkpoint-before-phase2`.

**Implemented and tested (22 unit tests + 3 autonomy tests):**
- **A. BTCC realism:** `btcc.py` now carries per-parameter provenance and presets (`conservative` default, `listed_vip0`, `adverse`; setting `exchange_preset`). BTCC's own pages conflict on fees (taker 0.045% / 0.048% / up to 0.06%), so the conservative end stays the default. Margin tiers, liquidation rules and ETH minimums were NOT found. See `docs/BTCC.md`.
- **B. AI research:** prompt now carries a structured memory digest (per-family tested/promising/top failure reasons, blocked ideas, binding constraints); proposals are DATA only, validated by a strict schema (families, grid ranges, filter whitelist and ranges, max 3 filters) before the normal pipeline; each AI hypothesis is stored with provenance; keys are redacted from prompts and logs. **Not exercised with a real Claude call: no `ANTHROPIC_API_KEY` is configured in this environment.**
- **C. New edges:** higher-timeframe trend filter (completed HTF bars only), efficiency-ratio trend/range regime filter, failed-breakout fade family, 5m timeframe for a higher-frequency test. All checked by the look-ahead test.
- **D. Statistical audit:** `scripts/null_calibration.py` runs the whole pipeline on block-resampled real bars (a) drift removed, (b) drift kept; robustness gates added (edge must survive removing the top 5% of trades, and must be positive in >=60% of calendar years); the 1,599 then-PROMISING strategies were rechecked from stored trades and 450 were downgraded. Every test-set access is recorded in `experiments` (stage `final`, once per strategy/market/timeframe).
- **Untouched forward set:** research data is frozen at `research_end` (2026-10-01). Newer days are fetched daily (`data_sync` job) and used only by `forward.py` (>= 8 weeks, >= 10 trades, positive mean R, trades in >= 75% of weeks). Only BACKTEST_QUALIFIED strategies can become PAPER_VALIDATED; PROMISING forward results are informational.
- **F. Persistence:** progress watchdog (queued work but no job completed for 15 min => restart worker; a heartbeat alone is not progress); incremental parse cache so a daily data sync does not re-parse all history; Windows Task Scheduler logon + 10-minute watchdog entries (`scripts/install_autostart.bat`, **untested on Windows**); dashboard shows seconds since the last completed experiment.

**Still open:** real BTCC margin/liquidation tiers; a real Claude hypothesis run; a long soak test on Windows; replacing Sidak with FDR/deflated-Sharpe; portfolio construction from genuinely new edges.

### Null-calibration results (real bars block-resampled, 24-bar blocks, 1,200 experiments each, 1h+4h, full pipeline)
| Null | PROMISING | reached final stage | BACKTEST_QUALIFIED |
|---|---|---|---|
| drift removed, seed 0 (pre robustness gates) | 10 (0.83%) | 39 | 0 |
| drift removed, seed 1 (current gates) | 23 (1.92%) | 66 | 0 |
| drift kept, seed 0 (pre robustness gates) | 58 (4.83%) | 105 | 0 |
| drift kept, seed 1 (current gates) | 45 (3.75%) | 98 | 0 |
Reading: with no skill available the PROMISING label still fires on roughly 1-5% of strategies (concentrated in smc_bos_pullback, vol_squeeze_breakout, taker_flow), while BACKTEST_QUALIFIED never fired in 4,800 null experiments.
On real data about 9% of strategies are PROMISING, so a PROMISING label is weak evidence and families that also fire on nulls deserve extra suspicion. The robustness gates did not lower the null rate much, so the gates are not what separates noise from signal: multiple-testing and the weekly/target gates are.
Forward path verified on real data: 8 top PROMISING strategies evaluated on 1.26 weeks of post-cutoff bars -> all `insufficient_data` (needs >= 8 weeks and >= 10 trades), as designed.


---
## Phase 3 (2026-10-11): zero-paid-API mode and the target-driven funnel
**Zero API cost.** `Settings.allow_paid_providers` defaults to false and paid providers (Anthropic, OpenAI-compatible) additionally need the environment variable `STRATEGYLAB_ALLOW_PAID=1`; either one missing blocks them in `ai.Router` before any network call (tests: `tests/test_zero_cost.py`). Default provider order is `["ollama"]` (free, local, used only if Ollama is installed, `allow_local_models` is true and >= 6 GB RAM is free). No model is required: deterministic research is the baseline. The dashboard and the live feed show the active research mode. No paid API call was ever made in this project (`ai_usage` is empty).
Optional free local model (not installed here): install Ollama and `ollama pull qwen2.5:3b` (~2 GB); it is used only for hypothesis proposals and every proposal still goes through schema validation and the identical deterministic pipeline.

**Funnel (every candidate, in order).**
- **A. Eligibility (`feasibility.py`, no backtest):** counts completed-trade slots (not candles) from the candidate's own signals with a 2-bar upper bound and an expected-hold spacing; rejects `insufficient_frequency` (< 1 trade/week even optimistically), `frequency_single_period` (> 60% of signals in one year), `cost_infeasible` (round-trip costs > 40% of the believable edge per stop unit), `infeasible_for_target` (gross edge per trade needed to reach the target > 0.8R after costs, using risk <= 10%/trade and leverage <= 50x), and `near_duplicate_failed_hypothesis` (same entry signals already failed 3 times). The target arithmetic: $1,000/month withdrawn with 50% of each profitable month retained means month-6 profit of $2,000, i.e. about +61% per month sustained for six months from $300.
- **B. Fast backtest**, **C. Robustness**, **D. Qualification:** unchanged gates (out-of-sample, walk-forward, sensitivity, cost stress, look-ahead, random-entry placebo, robustness, one-shot locked test, $300 Monte Carlo, Sidak multiple testing).
Every rejection is classified (`frequency`, `costs`, `economic_infeasibility`, `no_edge`, `instability`, `invalid`, `multiple_testing`, `duplicate`) and stored in the strategy summary (`failure_class`) and in the experiment row.

**Research priority.** Routine timeframes are now 5m, 15m, 1h (planner bonus 5m > 15m > 1h; 4h is excluded from routine research and only reachable through mutation of a parent). 1m is not enabled: 3.4M rows per symbol makes feature memory too heavy for 16 GB with the current in-memory design (needs a streaming rewrite first). Cells whose tests fail frequency/cost/economics screens >= 95% of the time (after >= 60 tests) are de-prioritised automatically. Legacy 4h results stay in the database untouched.

**Live feed.** The worker records the stage of the running test (A..D) and its parameters; the live artifact pushes a snapshot once a minute (queue, running test, finished tests).

## Phase 3b — Intelligent research engine (director, registry, GP)

**Audit baseline** (`docs/EFFICIENCY_REPORT.md`, regenerate with `scripts/efficiency_report.py`): 44.8k tests but only ~10k distinct hypotheses (exit-only duplicates dominated); 77% of compute went to 5m cells that mostly could not meet the cost/economic bar; only 10 of 1,336 legacy PROMISING survive today's frequency+economic screens (all 1h; 4h survivors are excluded by rule).

**Changes**
- *Pre-signal gates*: `feasibility.precheck_cost` (round-trip cost vs. stop/target geometry) and `novelty_check` (hypothesis registry: ≤3 exit variants per entry, "noise neighbourhood" after 3 failed adjacent-grid neighbours) run before any signal/backtest; rejections are persisted (`experiments.stage='eligibility'`, `precheck_rejections`, `hyp_registry`).
- *Signal cache* shared across exit variants; near-duplicate signal fingerprint guard.
- *Director* (`director.py`): Thompson-sampling over (family, symbol, timeframe) arms, reward from train/validation outcomes only per compute-second, exploit/evolve/explore shares, regime hints, stagnation mode (more exploration, forced direction change), human-readable explanation per batch (`director_batches`, `director_last`).
- *GP grammar* (`grammar.py`, family `gp_rule`): AND of ≤3 atoms with threshold grids, train-only univariate atom screen, niche-capped elites, crossover/mutation; MTF/regime filters via `FILTER_SPACE`.
- *Portfolio*: pool requires current eligibility; no combining losers; concentrated-profit gate.
- Qualification gates, locked-test one-shot, paid-API gating are unchanged. Live trading stays disabled.

**Status**: still 0 BACKTEST_QUALIFIED. The +61%/month compounding requirement remains likely unreachable on honest costs; the system keeps searching and reports negatives as-is.
