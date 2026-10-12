# NEXUS V2 — Autonomous Crypto-Perpetuals Research Platform

Local-first, **research-only** platform that continuously searches, backtests, validates and audits trading-strategy
hypotheses on ETH/BTC perpetual futures. **Live trading, live orders and withdrawals do not exist in this codebase**
(`nexus.LIVE_TRADING_ENABLED = False`; a test greps the source for order/withdraw endpoints).

> It does not promise a profitable strategy. Most hypotheses fail, and the system is built to say so with evidence.
> The $300 → $2,000/$6,000-per-month objectives are mission targets (667% / 2,000% per month on $300), not forecasts.

## Quick start
```
pip install -r requirements.txt        # numpy (+ pytest for tests). Python 3.12+ (developed on 3.13)
python -m nexus init
python -m nexus sync-data              # ~300 small checksum-verified files from data.binance.vision (~4 min)
python -m nexus start                  # launches a DETACHED supervisor + workers, starts the campaign
python -m nexus status                 # truthful health: healthy | idle | stalled | degraded | paused | stopped | dead
# dashboard: http://127.0.0.1:8765
python -m nexus pause | resume | stop  # stop = graceful (workers finish jobs); supervisor/dashboard/guardians stay up
python -m nexus shutdown               # stop everything, blocks until the supervisor has exited
python -m nexus test                   # full acceptance suite
```
Windows: `scripts\start_nexus.cmd`, `stop_nexus.cmd`, `status_nexus.cmd`; `python -m nexus install-startup` registers a visible,
per-user, non-elevated Scheduled Task (ON LOGON). On non-Windows hosts it prints "NOT CONFIGURED" and does nothing.

## Layout
| Path | Purpose |
|---|---|
| `nexus/config.py` | **Trusted** goals, thresholds, risk limits, cost model (hashed; guardians watch it) |
| `nexus/db.py` | SQLite WAL, migrations, integrity, backup/restore, incidents |
| `nexus/data.py` | resumable verified downloads, manifests, validation, aligned `Market`, split + holdout gate |
| `nexus/engine.py` | **Trusted** event-driven backtester (fees, slippage, funding, margin, liquidation, halts) |
| `nexus/strategies.py` | 30 families, grids, canonical identities, causal signals |
| `nexus/agents.py` | 10 research agents (disjoint scopes) |
| `nexus/guardians.py` | 3 independent guardians |
| `nexus/validation.py`, `qualify.py` | stages 3–6 and the pure qualification function |
| `nexus/scheduler.py`, `jobs.py`, `memory.py` | adaptive scheduler, durable queue, research memory |
| `nexus/runtime.py`, `worker.py` | supervisor, children, health |
| `nexus/server.py`, `ui/index.html` | dashboard (127.0.0.1 only) |
| `docs/` | architecture, operations, methodology, limitations, test evidence |

## Status (honest summary)
* 92 automated tests pass (0 skipped), including real-process fault-injection drills: see `docs/TESTING.md`.
* **No strategy is qualified; nothing here demonstrates a profitable edge.** In the first live run, in-sample "promising" strategies — and even a random-entry null model —
  were screened out by walk-forward validation. A protocol weakness found 15 minutes after launch was fixed under change control (`docs/CHANGELOG.md`).
* **Not done / unverified:** Windows execution & auto-start, BTCC contract/leverage verification (goals 2–4 stay "?"), a 7-day soak (`docs/SOAK_LOG.md`),
  paper-forward on real elapsed time. See `docs/KNOWN_LIMITATIONS.md`.
