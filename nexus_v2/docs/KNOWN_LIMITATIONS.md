# Known limitations (read before trusting anything)

1. **No candidate is qualified, and none is expected soon.** Real exploratory "promising" results so far failed walk-forward. Nothing here shows a
   profitable edge; the system's job is to reject false positives. $2,000/month on $300 equals +667%/month.
2. **Built and tested on Linux, not Windows.** Windows code paths (`msvcrt` lock, `DETACHED_PROCESS`, `CTRL_BREAK_EVENT`, `schtasks`,
   `GlobalMemoryStatusEx`) are implemented but **unexecuted**. Auto-start is therefore **not configured**.
3. **Seven-day reliability is unproven.** Only short runs and fault-injection drills have been executed. The container running this build is
   ephemeral; a runtime launched there does not outlive it.
4. **BTCC is unverified** (fees, margin tiers, funding, 250x/500x). Prices are Binance USDT-M proxy data.
5. **Funding uses the entry notional** (mark-price drift during the hold is ignored); intrabar order is unknowable from candles, so the
   conservative branch is used (stop before target; latest-possible exit time for funding).
6. **Candle data only**: no order-book, spread history, liquidation or open-interest data; spread/slippage are fixed assumptions.
7. **Holdout protection is procedural, not cryptographic**: agents run in the same codebase; the controls are access gating, an audit log and
   Guardian 3 re-checks. A malicious in-process agent could bypass them – the agents here are not adversarial code.
8. **Selection effects**: ~10⁵ experiments are screened on the same exploration data; the √(2 ln M) bar only controls the walk-forward stage.
   The holdout and paper-forward stages carry the real evidential weight. Paper-forward needs ≥30 real days of new data.
9. Python-level locks cannot stop a determined user from editing the DB by hand; Guardian 3 detects (not prevents) inconsistent claims.
10. GPU is detected but unused. No paid or free AI provider is configured; core operation does not need one.
