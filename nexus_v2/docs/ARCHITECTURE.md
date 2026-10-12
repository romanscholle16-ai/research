# Architecture Decision Record

**Decision: a modular monolith with separately supervised OS processes** (no broker, no microservices, no containers).
Rationale: a single Windows desktop with 16 GB RAM; every extra moving part is another thing that can die unattended.

```
                 ┌────────────── supervisor (lock, epoch, restart/backoff, commands) ──────────────┐
 CLI / dashboard │  web   scheduler   worker-1..N   guardian1  guardian2  guardian3                 │
   POST /control └──────────────┬──────────────────────────────────────────────────────────────────┘
                                ▼
                  SQLite (WAL)  jobs · experiments · strategies · hypotheses · candidates · validations ·
                  holdout_access · incidents · events · processes · checkpoints · exchange_specs · …
```
* **Durable state only in SQLite** (WAL, `BEGIN IMMEDIATE` write transactions, `busy_timeout` 30 s). No in-RAM queue.
* **Process ownership**: OS file lock (`fcntl`/`msvcrt`) + DB heartbeat; a *supervisor epoch* makes orphaned children of a dead
  supervisor exit when a new supervisor starts (no duplicate workers).
* **Idempotent persistence**: `UNIQUE(strategy_id, dataset_version, engine_version, phase)` – a job replayed after a crash cannot
  double count. All counters are derived from rows (`SELECT COUNT/SUM`), never incremented in memory.
* **Frontend is never the source of truth.** The UI only renders `/api/*` JSON computed from the database.
* **Trusted computing base** (engine, metrics, qualify, validation, config, data, indicators) is hashed; Guardian 3 pauses the campaign
  if it changes outside `nexus approve-code-change` (which requires a passing test run).

## Dependencies (why each)
| Dependency | Why |
|---|---|
| Python ≥3.12 stdlib (`sqlite3`, `http.server`, `subprocess`, `urllib`) | database, dashboard, supervisor, downloads – nothing to install |
| `numpy` | vectorised indicators and bootstrap/Monte-Carlo |
| `pytest` | tests only |
No pandas/Flask/psutil/numba: smaller Windows footprint, fewer wheels that can fail to install. No GPU is required (the GTX 1070 is
detected via `nvidia-smi` for display only).

## Dependency-free alternatives rejected
Redis/Celery (extra service), PostgreSQL (extra service; SQLite WAL is adequate for ≤3 workers), Docker (Windows-hostile for a desktop).

## Threading / process budget
`web + scheduler + 3 guardians + N workers (1–3, default 2)` ≈ 8 Python processes, ~1 GB. Workers exit and restart themselves above 1.5 GB RSS.
