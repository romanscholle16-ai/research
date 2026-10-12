# Operations: launch, stop, recovery, backup, diagnostics

| Task | Command |
|---|---|
| Launch (detached; survives closing the terminal / Claude Code) | `python -m nexus start` |
| Is it really alive and progressing? | `python -m nexus status` (exit code 0 healthy/idle/paused/stopped, 2 otherwise) or `GET /api/health` |
| Pause / resume | `python -m nexus pause` / `resume` (workers idle, scheduler stops enqueuing) |
| Graceful research stop | `python -m nexus stop` (workers finish current job; supervisor, dashboard, guardians stay up) |
| Full shutdown | `python -m nexus shutdown` (blocks until the supervisor has exited) |
| Restart one worker | `python -m nexus restart-worker worker-1` or the System Health page |
| Manual checkpoint | `python -m nexus backup` (SQLite online backup, integrity-verified) |
| Integrity check | `python -m nexus integrity` |
| Restore | `python -m nexus shutdown && python -m nexus restore var/backups/<file>.db` (current DB kept as `.pre-restore-*`) |
| Controlled change to trusted code | edit → `python -m nexus approve-code-change "<reason>"` (runs the full tests; baseline updated only if they pass) |
| Pre-launch baseline reset (only while no research exists) | `python -m nexus rebaseline` |
| Project-state file | `python -m nexus state-report` → `PROJECT_STATE.json` |

## Where things are
`var/` (override with `NEXUS_HOME`): `nexus.db` (+WAL), `data/` (raw zips, `*_1h.npz`), `logs/` (rotating 5 MB×5 per component, `*.out` for
child stdout), `backups/` (12 newest periodic), `run/` (supervisor.lock, web token). Dashboard: `http://127.0.0.1:8765` (`NEXUS_PORT`).

## Recovery behaviour
* Worker/scheduler/guardian/web crash → supervisor restarts it with exponential backoff (2^n s, cap 300 s; >5 exits in 10 min ⇒ crash-loop
  incident and 5-minute throttle). Its leased jobs are requeued with an attempt counted; after `max_attempts` a job is **quarantined**, not retried forever.
* Supervisor crash → dashboard/CLI report **dead** immediately; Guardian 1 (orphaned but alive) relaunches it (≤ once / 2 min); on start the new
  supervisor requeues interrupted jobs, runs an integrity check, takes a checkpoint if none in 6 h, and publishes a new *epoch* so old children exit.
* Interrupted downloads resume via HTTP Range into `*.part`, are SHA-256 verified against the archive's `.CHECKSUM`, then renamed atomically.
* Database corruption → quick check every 5 min (full hourly): campaign **paused**, critical incident; restore from a verified checkpoint.
* Low disk (<3 GB default) → campaign paused. Memory > 88% → throttle (workers idle) until relieved.
* Nothing is silently deleted: negative results, incidents and holdout accesses are append-only.

## Windows notes
* Auto-start: `python -m nexus install-startup` creates a **visible** per-user Scheduled Task (`NEXUS_V2_Research`, ON LOGON, limited rights).
  It cannot run before sign-in or while the PC is off; for a 7-day run configure Windows to **not sleep** (Power & sleep → Never) and keep
  the PC on mains power. NEXUS does not change system power settings.
* The supervisor is launched `DETACHED_PROCESS` (visible in Task Manager as `python.exe`); children use `CREATE_NEW_PROCESS_GROUP`
  and are stopped with `CTRL_BREAK_EVENT`.
* **Windows paths are implemented but could not be executed in the build environment (Linux)** – see Known Limitations.

## Seven-day soak procedure (to be run by the user)
1. Disable sleep; `python -m nexus start`; note the start time and `python -m nexus state-report`.
2. Daily: `python -m nexus status`, check Guardians page, Event Timeline (incidents), disk, `python -m nexus backup`.
3. After 7×24 h: review incidents (recurred/unresolved), restart count per process, `integrity`, experiment/candidate counters, any `guardian_log` failures.
4. Only then record the result in `docs/SOAK_LOG.md`. Until that file contains a completed run, **seven-day reliability is unproven**.
