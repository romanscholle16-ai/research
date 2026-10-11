"""Print an honest research summary from the database (real-data only unless --synthetic)."""
import sys, os, json, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB
syn = 1 if "--synthetic" in sys.argv else 0
d = DB()
print("cycles (all):", d.get("cycles"), "| status counts:", d.counts(syn))
c = collections.Counter()
for r in d.q("SELECT reasons FROM lessons l JOIN strategy_status s ON s.strategy_id=l.strategy_id AND s.symbol=l.symbol AND s.timeframe=l.timeframe WHERE s.synthetic=?", (syn,)):
    for x in json.loads(r["reasons"]): c[x] += 1
print("top rejection reasons:", c.most_common(8))
fin = collections.Counter()
for r in d.q("SELECT summary_json sj, status FROM strategy_status WHERE synthetic=? AND status!='REJECTED'", (syn,)):
    for x in json.loads(r["sj"]).get("reasons", []): fin[x] += 1
print("why non-rejected strategies are not QUALIFIED:", fin.most_common(8))
best = []
for r in d.q("SELECT ss.strategy_id sid,symbol,timeframe,summary_json sj,spec_json sp FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.synthetic=? AND status!='REJECTED'", (syn,)):
    f = json.loads(r["sj"]).get("final")
    if f and f.get("capital_all"):
        best.append((f["capital_all"]["monthly_withdraw_any"]["1000"]["p"], r["sid"], r["symbol"], r["timeframe"], json.loads(r["sp"])["family"], f["test"]["mean_R"], f["test"]["n"], f["multiple_testing"]["adj_p"], f["capital_all"]["final_wealth_pct"]["50"], f["capital_all"]["ruin"]["p"]))
best.sort(reverse=True)
print("top by P(>= $1000/month capacity within 6 months) [month-block bootstrap, NOT a forecast]:")
for b in best[:8]: print("  P=%.3f %s %s %s %s testMeanR=%.2f n=%d adj_p=%.3f medWealth=$%.0f ruin=%.3f" % b)
