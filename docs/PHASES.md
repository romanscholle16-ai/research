# Development phases (objective → implemented → verified → limitations → next)

| # | Objective | Implemented | Verified by | Known limits | Next |
|---|---|---|---|---|---|
| 0 | Audit existing StrategyResearchLab | Repo contained only an empty README → greenfield | `git ls-files` | local project not accessible from the cloud session | port/audit the local project if provided |
| 1 | Persistent autonomous engine | `db.py` job queue, `service.py` watchdog, `worker.py`, crash/stall/timeout/RSS recovery, pause/stop | `tests/test_autonomy.py` (100+ cycles, crash, hang, exception, SIGKILL restart, quota, RSS limit, pause/resume) | synthetic data; short run ≠ 24/7 proof | long soak run on the Windows box |
| 2 | Data | `data.py` downloader/auditor/resampler for 1m→5m/15m/1h/4h + funding; synthetic fixtures flagged | real download of BTC/ETH 2019-09→present | no OI/liquidation/L2/bid-ask; Binance as BTCC proxy | add vendor data where legitimately obtainable |
| 3 | Backtester | causal features, conservative fills, funding, costs scaling with notional | `tests/test_core.py` (stop-first, gap fills, cost scaling, causality of all 14 families) | bar-level only | tick/trade-level replay for 1m strategies |
| 4 | Validation | screen/validate/final, WFO, sensitivity, cost stress, look-ahead detector (proven to catch a cheating signal), one-shot locked test, Šidák | tests + synthetic-edge run through all stages | thresholds are judgement calls (documented in code/config) | calibrate on null-hypothesis (shuffled) data |
| 5 | $300 capital engine | sizing, leverage, isolated/cross liquidation, min-lot, monthly 50 %-retention withdrawals, Wilson CIs, rolling windows | unit tests (liquidation maths, retention, lot sizes) | bootstrap ≠ forecast | add regime-conditional bootstrap |
| 6 | Agents / discovery | 14 families with rationale + falsification, UCB explore, near-miss mutation, lessons, dedupe, optional AI proposals (validated) | tests + live runs | AI path exercised with mock provider only (no API key in build env) | richer novel-hypothesis grammar |
| 7 | Dashboard | `dashboard.py` + `dashboard.html`, controls, leaderboard, capital fan chart, offline last-known | API smoke tests; JS syntax check | visual QA on Windows not done here | UI polish, settings validation |
| 8 | Paper trading | **not built** (PAPER_VALIDATED is never auto-assigned) | – | – | forward paper-trade module |
