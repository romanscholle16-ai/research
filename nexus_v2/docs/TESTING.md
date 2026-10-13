# Test evidence (exact outcomes)

Final full run (`python -m nexus test`, Linux, Python 3.13): **92 passed, 0 failed, 0 skipped** in 208 s (recorded in the `test_evidence` table).
Run once more by the user with `python -m nexus test`; on Windows the POSIX-only drill steps (`SIGKILL`, `pgrep`, `/proc`) will need adaptation – see Known Limitations.

| Gate | Evidence (file) | What is actually verified |
|---|---|---|
| A repo/env | this repo, `requirements.txt`, git history | inspected env (Linux/py3.13/4 cores/no GPU), checkpoint commits |
| B DB & persistence | `tests/test_db_and_queue.py` (10) | migrations idempotent, WAL, rollback, backup/restore, corrupt backup refused, job retry→quarantine, dead-worker job reclaim, live slow job **not** killed, unique experiment identity / replay does not double count, counters reconcile with rows, baseline freeze |
| C data & engine | `tests/test_engine.py` (18), `tests/test_data.py` (11) | hand-calculated fees/P&L/funding/liquidation/gap/tie-break/slippage direction/cash-flow separation/leverage validity (250x, 500x ⇒ invalid), determinism, next-open fills; dupes/gaps/bad rows, µs timestamps, aggregation boundaries, **interrupted download resume + checksum mismatch rejection**, fixed splits survive new bars, purge gap, holdout denial/single-look |
| C look-ahead | `tests/test_strategies_agents.py` | **every family** causal on truncated prefixes; future-bar mutation cannot change past signals |
| D qualification | `tests/test_validation_guardians.py` (21) | planted-edge synthetic world passes all static gates and then **waits** for paper-forward; full lifecycle to *qualified* only via Guardian 3; noise data never qualifies; synthetic/funding-missing data cannot qualify; rogue `status='qualified'` write is revoked; threshold change ⇒ evidence stale; multiple-testing bar independent of arrival order; exit-rule drift not mistaken for entry skill; holdout needs significance; goal 6 needs holdout; controlled config change + holdout stays spent |
| E agents/scheduler | `tests/test_strategies_agents.py` (11) | 10 agents / 3 guardians, disjoint scopes, canonical identity, duplicate rejection, budgets+pacing, reasons logged, negative results preserved, adversarial agent only *recommends*, hybrid idles without components |
| F dashboard | `tests/test_dashboard.py` (9) | all 16 API routes, all 15 goal names + required fields, no unverified ✓, counters = SQL, **dead runtime never healthy**, POST token/host/bounds, ad-hoc backtest cannot reach holdout, simulator deposits ≠ profit, **headless-Chromium DOM**: nav + all 15 names + HTML escaping |
| G recovery drills | `tests/test_runtime_drills.py` (13) | real processes: worker `kill -9` mid-task; supervisor `kill -9` ⇒ status **dead** ⇒ clean restart + orphan supersession; duplicate-supervisor refusal; graceful stop/resume; poison job quarantine; stalled job; memory & disk breach; DB corruption + restore; AI provider down ⇒ fallback + backoff; no paid-AI/live-order code paths |
| H e2e campaign | `test_end_to_end_campaign_and_independence_from_caller` | start→experiments persist→validation→API; launcher exits, runtime keeps progressing |
| I scale-up | `scripts/scale_probe.py`, `var/scale_probe.json` | budget 150→300→600 /agent/h = 30→50→100 experiments/min at 3.5%→2.7%→5.3% CPU, ≤6% RAM, 0 incidents; workers stay at 2 (queue always empty ⇒ more workers add nothing); default kept at 150 |
| J readiness | see project_state.json, `nexus status` | independent startup, persistent state, checkpoint, safe stop/restart, health reporting, no live-order code, no paid API |

## Defects found by this process (all fixed, regression-tested)
See `docs/CHANGELOG.md`. The most important: a chance candidate passed walk-forward **and** the holdout in the first live run because the
multiple-testing bar depended on arrival order and the holdout had no significance test.

## Not covered / not run
Windows execution of any kind; a 7-day soak; paper-forward on real elapsed time; BTCC verification.
