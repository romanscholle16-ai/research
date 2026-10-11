"""Apply the robustness gates (few-trade dependence, single-year dependence) to already-promoted strategies from their stored trades."""
import sys, os, json, time; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB
from strategylab.backtest import Trades
from strategylab import validation as V
d = DB(); n = rej = 0
for r in d.q("SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, ss.summary_json sj, t.trades_json tj, s.family fam FROM strategy_status ss JOIN trades t ON t.strategy_id=ss.strategy_id AND t.symbol=ss.symbol AND t.timeframe=ss.timeframe AND t.stage='all' JOIN strategies s ON s.id=ss.strategy_id WHERE ss.synthetic=0 AND ss.status='PROMISING'"):
    sm = json.loads(r["sj"])
    if "robustness" in sm.get("final", {}): continue
    rb, rs = V.robustness(Trades.from_json(json.loads(r["tj"]))); n += 1
    sm["final"]["robustness"] = rb
    status = "PROMISING"
    if rs:
        status = "REJECTED"; sm["reasons"] = rs + sm.get("reasons", []); rej += 1
        d.x("INSERT INTO lessons(ts,family,symbol,timeframe,reasons,strategy_id) VALUES(?,?,?,?,?,?)", (time.time(), r["fam"], r["sy"], r["tf"], json.dumps(rs), r["sid"]))
    d.x("UPDATE strategy_status SET status=?, summary_json=?, updated=? WHERE strategy_id=? AND symbol=? AND timeframe=?", (status, json.dumps(sm, default=float), time.time(), r["sid"], r["sy"], r["tf"]))
print(f"checked {n} promising; downgraded {rej} to REJECTED")
