"""Export a JSON progress snapshot (real-data research only) for the tracker artifact.  Usage: export_status.py [out.json]"""
import sys, os, json, time, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB
from strategylab import resources

d = DB(); now = time.time()
hb = d.get("service_heartbeat", 0)
counts = d.counts(0)
reasons = collections.Counter()
for r in d.q("SELECT l.reasons FROM lessons l JOIN strategy_status s ON s.strategy_id=l.strategy_id AND s.symbol=l.symbol AND s.timeframe=l.timeframe WHERE s.synthetic=0"):
    for x in json.loads(r["reasons"]): reasons[x] += 1
blockers = collections.Counter()
for r in d.q("SELECT summary_json sj FROM strategy_status WHERE synthetic=0 AND status!='REJECTED'"):
    for x in json.loads(r["sj"]).get("reasons", []): blockers[x[:-5] if x.endswith("_test") else x[:-4] if x.endswith("_all") else x] += 1
cands = []
for r in d.q("SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, ss.status st, ss.summary_json sj, s.family fam FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.synthetic=0 AND ss.status!='REJECTED'"):
    sm = json.loads(r["sj"]); f = sm.get("final") or {}
    c = f.get("capital_all") or {}
    if not c: continue
    cands.append(dict(id=r["sid"], inst=r["sy"], tf=r["tf"], family=r["fam"], status=r["st"], test_n=f["test"]["n"], test_R=round(f["test"]["mean_R"], 3),
                      weekly=round(f.get("weekly_coverage_test", 0), 2), p1k=round(c["monthly_withdraw_any"]["1000"]["p"], 4), ruin=round(c["ruin"]["p"], 4),
                      adj_p=round(f["multiple_testing"]["adj_p"], 4), med_wealth=round(c["final_wealth_pct"]["50"]), why=[x for x in sm.get("reasons", [])][:4]))
cands.sort(key=lambda x: (x["status"] != "BACKTEST_QUALIFIED", -x["p1k"]))
hist = d.q("SELECT CAST(finished/1800 AS INT)*1800 AS t, COUNT(*) n FROM experiments WHERE synthetic=0 AND finished IS NOT NULL GROUP BY 1 ORDER BY 1")
cum, series = 0, []
for h in hist: cum += h["n"]; series.append([h["t"], cum])
out = dict(generated=now, control=d.get("control", "stopped"), service_age_s=(now - hb) if hb else None, cycles=d.get("cycles", 0), counts=counts,
           experiments=counts.get("experiments", 0), qualified=counts.get("BACKTEST_QUALIFIED", 0),
           portfolios=d.q("SELECT status, COUNT(*) n FROM strategy_status WHERE symbol='MULTI' GROUP BY 1"),
           candidates=cands[:8], reasons=reasons.most_common(8), blockers=blockers.most_common(6), series=series[-96:],
           ai_state=d.get("ai_state", "unknown"), blocker=d.get("blocker"),
           errors_1h=d.q("SELECT COUNT(*) n FROM events WHERE level='error' AND ts>?", (now - 3600,))[0]["n"],
           restarts_24h=d.q("SELECT COUNT(*) n FROM events WHERE message LIKE 'worker exited%' AND ts>?", (now - 86400,))[0]["n"],
           queue=d.q("SELECT kind, COUNT(*) n FROM jobs WHERE status='queued' GROUP BY 1"),
           data=d.q("SELECT symbol,timeframe,rows,start_ts,end_ts FROM datasets WHERE synthetic=0 ORDER BY symbol,timeframe"))
json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else "/dev/stdout", "w"), default=float)
