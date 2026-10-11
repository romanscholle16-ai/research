# Autonomy acceptance test (`tests/test_autonomy.py`) — SYNTHETIC fixtures
What it does: starts the **real** `service` → `worker` subprocess pair against deterministic synthetic data (flagged `synthetic=1`
everywhere, excluded from real-research counters) with a scripted mock AI provider, then without any intervention:
* injects a worker **crash** (jobs #7, #120), a **hang** (#15 → watchdog kill), a job **exception** (#20 → bounded retry);
* mock AI returns valid proposals, garbage, an invalid-family proposal, and **quota exhaustion** (→ cooldown + deterministic fallback);
* **SIGKILLs the whole service and worker** after ≥60 cycles (power-loss/reboot analogue), restarts the service, which resumes from the
  persisted `running` state, requeues interrupted jobs, and completes ≥100 further cycles (total ≥160);
* checks: no duplicate (strategy, instrument, timeframe), nothing left `running` after Stop, ≤3 permanently failed jobs, results persisted,
  no pure-noise strategy reaches `BACKTEST_QUALIFIED`;
* separately: RSS limit makes the worker exit with code 75 (restart path); Pause halts progress and Resume restores it.
Bug found and fixed by this test: an exception raised inside a job-error handler left the job `running` forever (watchdog could not see it
because the worker heartbeat kept ticking). Now the outer handler fails the claimed job and the service also reclaims jobs with stale job heartbeats.
Limits: minutes-long synthetic runs; says nothing about alpha or multi-day stability. Run a soak test on the target Windows machine.
