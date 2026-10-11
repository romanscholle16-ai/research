# StrategyLab — autonomous crypto-futures strategy discovery (RESEARCH ONLY)

A persistent Python research service that continuously invents, backtests, validates and records BTCUSDT/ETHUSDT
perpetual-futures strategies against one objective: grow a **$300** account so that **≥ $1,000/month** can be
withdrawn (while retaining ≥ 50 % of every positive month) within six months. The target is a *research objective, not a
promise*; nothing here is trading advice. **No live trading code exists** (`config.LIVE_TRADING_ENABLED = False`, no order
or withdrawal path in the code base; a test asserts this).

> The repository you pointed me at contained only an empty README — there was no existing *StrategyResearchLab* to audit or
> reuse, so everything here is new. If your local project exists, tell me its path/repo and I'll audit it and port
> reusable parts instead of duplicating.

## Quick start (Windows)
```
pip install -r requirements.txt
python scripts/fetch_data.py BTCUSDT          # one-off: download real history (public archive) – also ETHUSDT 2019-11
scripts\start_dashboard.bat                   # opens http://127.0.0.1:8765 ; press "Start Research"
scripts\install_autostart.bat                 # OPTIONAL: restart service at logon
```
Normal use needs no command line after data download: the dashboard launches/controls the background service.
State lives in `%USERPROFILE%\StrategyLab` (override with `STRATEGYLAB_HOME`): `research.db`, `settings.json`, `data/`.

## Architecture
| Module | Role |
|---|---|
| `service.py` | Persistent supervisor process: spawns/monitors worker, watchdog (heartbeat + job timeout), crash backoff (bounded, never gives up permanently), pid lock |
| `worker.py` | Claims jobs from SQLite queue (`plan` → `experiment`), checkpoint = every finished experiment is committed; RSS limit → clean restart |
| `agents.py` | Research Scientist, Strategy Inventor, Evolution Specialist, Research Supervisor (UCB bandit over family×instrument×timeframe, near-miss mutation, dedupe) |
| `engine.py` + `backtest.py` + `validation.py` + `capital.py` | Backtesting Engineer, Statistical Validator, Risk & Leverage Specialist, $300 capital engine |
| `ai.py` | Provider-independent AI (Anthropic → OpenAI-compatible → Ollama → deterministic), cooldowns, retries, budgets, token/cost log. AI only *proposes*; every proposal is schema-validated and run through the same deterministic pipeline |
| `db.py` | SQLite WAL research memory: hypotheses, strategies, experiments, trades, lessons, jobs, datasets, AI usage, events |
| `dashboard.py/.html` | Localhost dashboard: Start/Pause/Resume/Stop, leaderboard, capital simulation, AI/resource status, offline last-known view |

AI is optional: with no key/quota the deterministic generator keeps researching; AI-assisted planning resumes automatically
when a provider returns (set `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`, or run Ollama).

## Validation pipeline (per strategy × instrument × timeframe)
1. **screen** (train 50 %): trade count, net expectancy in R, t-stat, PF; failure reasons recorded (`cost_dominated`, `no_edge`, `too_few_trades` …).
2. **validate** (next 25 %): out-of-sample, 6-fold consistency, anchored walk-forward *optimisation*, parameter-neighbour sensitivity, 1.5×/2× cost stress, **look-ahead test** (signals recomputed on truncated data must be identical).
3. **final**: risk/leverage/margin-mode fitted on train+val only → **one-shot locked test (last 25 %)** → $300 Monte-Carlo (month-block bootstrap + real rolling 6-month windows) → weekly-trade coverage → **Šidák multiple-testing** over *every* strategy ever tested.
Statuses: `REJECTED` → `PROMISING` → `BACKTEST_QUALIFIED` (all gates) → `PAPER_VALIDATED` / `LIVE_ELIGIBLE` (never assigned automatically; need forward paper trading and human review).

See `docs/` for phases, BTCC assumptions, limitations and the acceptance test.
