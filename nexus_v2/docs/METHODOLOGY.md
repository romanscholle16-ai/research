# Backtesting & validation methodology

## Execution model (`nexus/engine.py`)
* Signal decided at the **close** of bar *i*; entry at the **open** of bar *i+1* with adverse slippage + half-spread.
* Stops/targets are checked from the entry bar onward. **Same-bar stop+target ⇒ stop fills first.** Gap through a stop ⇒ fills at the
  worse open. Targets never get price improvement. Signal exits fill at the next open.
* Fees: taker on both legs (assumed 0.06%/side). Slippage 2 bps + 0.5 bps half-spread per side (assumed).
* Funding: real historical funding events (Binance archive, proxy venue) accrue to positions held across the event; long pays when the
  rate is positive. If history is missing the run assumes a *cost* and is flagged non-qualifying.
* Isolated margin. `1/L ≤ MMR + 2·fees` ⇒ the simulation is **invalid** (reported, no trades). Liquidation loses the whole margin.
* Sizing: risk 1% of equity at the stop (costs included in the risk denominator), capped at `equity × leverage`, rounded to the qty step.
* Account halts: −5% daily loss (no new entries that UTC day) and −40% from peak (halt for the run).
* Deposits/withdrawals are a separate ledger; they never enter trading P&L (tested).
* Hand-calculated reference cases: `tests/test_engine.py`.

## Data split (fixed at first sight, stored in `data_splits`)
`[0, 50%)` exploration (agents may tune here) · `[50%, 80%)` walk-forward (purged by `max_hold` bars) · `[80%, 100%]` **protected holdout** ·
bars appended after the holdout end are **forward-only** (paper-forward). Appending data never re-opens settled experiments
(identity uses a hash of the exploration region only).

## Stages
1. **S1** quick screen (first quarter of exploration) → 2. **S2** development (full exploration): ≥80 trades, expectancy>0, t≥1.5, PF≥1.05.
3. **Walk-forward**: 4 consecutive folds after the purge gap; pooled OOS trades ≥60; expectancy>0; block-bootstrap 90% CI lower bound >0;
   t ≥ max(1.2, √(2 ln M)) with **M = max(candidates seen, 50)** — a pre-registered floor, so the bar is ≥ 2.80 from the very first candidate (an arrival-order-dependent bar let a chance candidate through in the first live run; see CHANGELOG); ≥60% positive folds.
4. **Robustness** (development data): ≥120 trades; ≥60% of parameter neighbours profitable; positive under 2× fees+slippage and 3× funding;
   positive after removing the 3 best trades; non-negative in ≥2 of 3 volatility regimes; Monte-Carlo block bootstrap: 95th-pct drawdown ≤35%,
   risk of ruin (equity ≤50% of start in ~6 months of trades) ≤5%; no liquidations; **out-of-sample randomisation test** on the purged walk-forward region: 200 random-entry runs with the *same* stops/targets/holds must do worse than the candidate (p ≤ 0.05) — on trending data random-direction entries with stops are themselves profitable, so exit-rule drift is not accepted as entry skill.
5. **Protected holdout**: single look per candidate, only after stages 3–4 pass, only via `validation.holdout_stage`, global budget of 12 looks,
   every attempt (granted or denied) logged. Pass: ≥30 trades, expectancy>0, **t ≥ 1.0**, PF≥1, drawdown ≤35%.
6. **Paper-forward**: bars after the holdout end; ≥30 days, ≥10 trades, expectancy>0, PF≥1. Needs real elapsed time.
7. **Qualified** only when stages 3–6 pass *and* Guardian 3 independently reproduces the S2 result, confirms exactly one holdout look and a
   causality probe. `qualify.assess()` is a pure function of stored evidence; evidence recorded under different thresholds/engine is "stale".

All thresholds live in `config.THRESHOLDS`, are hashed, and are listed with their rationale on the dashboard (Validation page).

## Honest limitations of the statistics
Agents see exploration-region metrics only for refinement, but walk-forward pass/fail is visible to the scheduler's bookkeeping, so
walk-forward is "pseudo out-of-sample" after many candidates; the holdout and forward stages are the real out-of-sample evidence.
Trades from overlapping holds are serially dependent – hence block bootstraps rather than i.i.d. t-tests alone.
