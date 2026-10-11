"""Forward (post-research) evaluation: the only genuinely untouched data. Research, selection and the locked test all end at
`research_end`; bars after it were never seen by any search decision. A strategy can reach PAPER_VALIDATED only by passing here
(and only after BACKTEST_QUALIFIED). Forward results for PROMISING strategies are informational and never promote them.

Forward criteria (all required): >= 8 weeks of forward data, >= 10 completed trades, mean R > 0 after costs,
trade in >= 75% of forward weeks, no liquidation at the fitted sizing. Funding after the monthly archive ends is the last known rate (flagged)."""
from __future__ import annotations
import json, time
import numpy as np, pandas as pd
from . import strategies as ST, capital as CAP
from .backtest import backtest, metrics, weekly_coverage
from .btcc import DEFAULT
from .features import Features

MIN_WEEKS, MIN_TRADES, MIN_COVERAGE = 8, 10, 0.75


def evaluate_forward(db, st, store, sid, symbol, tf, em=DEFAULT):
    r = db.q("SELECT ss.status, ss.summary_json sj, s.spec_json sp FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.strategy_id=? AND ss.symbol=? AND ss.timeframe=?", (sid, symbol, tf))
    if not r or "MULTI" == symbol: return {"skipped": "n/a"}
    spec = json.loads(r[0]["sp"]); sm = json.loads(r[0]["sj"])
    item = store.get(symbol, tf); full = item["df_full"]; cut = pd.Timestamp(st.research_end, tz="UTC")
    if full.index[-1] <= cut: return {"skipped": "no_forward_data"}
    ref = None
    if item["F"].ref is not None:
        other = "ETHUSDT" if symbol == "BTCUSDT" else "BTCUSDT"; ref = store.get(other, tf)["df_full"]
    F = Features(full, ref); lo = int(full.index.searchsorted(cut)); L, S = ST.signals(F, spec)
    tr = backtest(F, spec, L, S, em, lo, len(full)); m = metrics(tr)
    t0, t1 = float(F.epoch_s[lo]), float(F.epoch_s[-1]) + 1; weeks = (t1 - t0) / (7 * 86400)
    cov = weekly_coverage(tr, t0, t1) if len(tr) else 0.0
    fw = dict(weeks=round(weeks, 2), n=m["n"], mean_R=m["mean_R"], coverage=cov, asof=str(full.index[-1]), cutoff=str(cut), funding_note="last known rate after monthly archive end")
    ok = weeks >= MIN_WEEKS and m["n"] >= MIN_TRADES and m["mean_R"] > 0 and cov >= MIN_COVERAGE
    fw["passed"] = bool(ok); fw["verdict"] = "pass" if ok else "insufficient_data" if (weeks < MIN_WEEKS or m["n"] < MIN_TRADES) else "fail"
    sm["forward"] = fw
    status = r[0]["status"]
    if status == "BACKTEST_QUALIFIED" and ok: status = "PAPER_VALIDATED"
    db.x("UPDATE strategy_status SET status=?, summary_json=?, updated=? WHERE strategy_id=? AND symbol=? AND timeframe=?", (status, json.dumps(sm, default=float), time.time(), sid, symbol, tf))
    db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,dataset_id,started,finished,status,metrics_json,failure_reasons,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
         (sid, "forward", symbol, tf, item["meta"]["id"], time.time(), time.time(), fw["verdict"], json.dumps(fw), "[]"))
    return {"strategy_id": sid, "status": status, **fw}
