"""Re-evaluate historical entry hypotheses under the 250x/500x execution model, DEVELOPMENT DATA ONLY (train+validation; the locked test is never read).
Question answered: did the old funnel wrongly reject candidates (a) validated entries whose exits were too wide for 250x, (b) entries killed by the cost/target priors
before any backtest?  Each entry is re-paired with exits whose stop fits inside the 250x liquidation distance (stop 0.14-0.22% of price), then backtested
under `user_500x_maker` (passive fills, taker stops).  Counts viable-on-dev entries; the best few are queued through the normal funnel (which owns the locked test).
Usage: python scripts/reeval_old.py [n_validated=250] [n_prior_rejected=400] [queue=30]"""
import sys, json, time, collections
import numpy as np
from strategylab.db import DB
from strategylab.config import Settings
from strategylab.engine import DataStore
from strategylab.worker import make_loader
from strategylab import btcc, strategies as ST, validation as V, feasibility as FE, refine as RF

nv = int(sys.argv[1]) if len(sys.argv) > 1 else 250; npr = int(sys.argv[2]) if len(sys.argv) > 2 else 400; nq = int(sys.argv[3]) if len(sys.argv) > 3 else 30
db = DB(); st = Settings.load(); store = DataStore(st, make_loader()); em = btcc.get_model("user_500x_maker")
rng = np.random.default_rng(11)

def collect(sql, limit, args=()):
    seen, out = set(), []
    for r in db.q(sql, args):
        sp = json.loads(r["sj"]); k = (ST.entry_key(sp), r["sy"], r["tf"])
        if k in seen or r["tf"] == "4h": continue
        seen.add(k); out.append((r["sy"], r["tf"], sp, r["sid"]))
    idx = rng.permutation(len(out))[:limit]; return [out[i] for i in idx]

A = collect("""SELECT DISTINCT s.id sid, s.spec_json sj, e.symbol sy, e.timeframe tf FROM experiments e JOIN strategies s ON s.id=e.strategy_id
               WHERE e.stage='validate' AND e.status='pass' AND e.synthetic=0""", nv)
B = collect("""SELECT DISTINCT s.id sid, s.spec_json sj, e.symbol sy, e.timeframe tf FROM experiments e JOIN strategies s ON s.id=e.strategy_id
               WHERE e.stage='eligibility' AND e.status='fail' AND e.synthetic=0 AND (e.failure_reasons LIKE '%cost_infeasible%' OR e.failure_reasons LIKE '%infeasible_for_target%')
               AND e.failure_reasons NOT LIKE '%insufficient_frequency%' ORDER BY e.id DESC LIMIT 20000""", npr)

def pair_exits(F, bounds, spec):
    af = F.atr_frac_med(*bounds["train"], 14)
    if not (af == af and af > 0): return []
    out = []
    for tf_ in (0.0014, 0.0018, 0.0022):
        sa = tf_ / af
        if sa < 0.25: continue
        for rr in (1.5, 3.0, 5.0):
            s = {k: v for k, v in spec.items()}; s.update(stop_atr=round(float(sa), 3), target_rr=rr, max_hold=48, atr_n=14)
            if not ST.validate_spec(s): out.append(s)
    return out

res = dict(A=collections.Counter(), B=collections.Counter()); keep = []; t0 = time.time()
for grp, items in (("A", A), ("B", B)):
    for sy, tf, spec, sid in items:
        it = store.get(sy, tf); F, bounds = it["F"], it["bounds"]; tlo, thi = bounds["train"]; vlo, vhi = bounds["val"]
        try: sig = ST.signals(F, spec)
        except Exception: res[grp]["signal_error"] += 1; continue
        best = None; res[grp]["entries"] += 1
        for s in pair_exits(F, bounds, spec):
            ea = FE.stage_a(F, s, bounds, em, st, sig)
            if ea["decision"] != "pass": res[grp]["stageA_fail:" + (ea["reasons"][0] if ea["reasons"] else "?")] += 1; continue
            a = V.run(F, s, em, tlo, thi); b = V.run(F, s, em, vlo, vhi)
            if len(a) < 60 or len(b) < 20: res[grp]["too_few_trades"] += 1; continue
            ra, rb = float(a.R.mean()), float(b.R.mean()); allR = np.r_[a.R, b.R]; t = float(allR.mean() / (allR.std(ddof=1) / np.sqrt(len(allR))))
            if ra > 0 and rb > 0 and t > 2.0 and (best is None or t > best[0]): best = (t, s, ra, rb, len(a), len(b))
            res[grp]["backtested"] += 1
        if best: res[grp]["viable_on_dev"] += 1; keep.append((best[0], sy, tf, best[1], best[2:], grp, sid))
        if time.time() - t0 > 3000: break
keep.sort(key=lambda x: -x[0]); q = 0
for t, sy, tf, s, info, grp, sid in keep[:nq]:
    db.enqueue("experiment", dict(spec=s, symbol=sy, timeframe=tf, note=f"reeval250:{sid[:10]}"), priority=3); q += 1
lines = [f"# Re-evaluation of old entries under the 250x/500x model (dev data only; {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})\n",
         "Cost model `user_500x_maker`; exits re-paired so the stop fits inside the 250x liquidation distance; viable = positive mean R in BOTH train and validation and t>2 on dev.\n"]
for g, name in (("A", "dev-validated entries (old funnel passed them)"), ("B", "entries rejected by cost/target priors before any backtest")):
    lines.append(f"## {name}\n" + "\n".join(f"- {k}: {v}" for k, v in sorted(res[g].items())) + "\n")
lines.append(f"Queued into the normal funnel (which runs validation + the one-shot locked test): **{q}**\n\nTop dev results:\n")
for t, sy, tf, s, info, grp, sid in keep[:10]: lines.append(f"- {grp} {s['family']} {sy} {tf} stop_atr={s['stop_atr']} rr={s['target_rr']}: dev t={t:.2f}, train R={info[0]:+.3f} (n={info[2]}), val R={info[1]:+.3f} (n={info[3]})")
open("docs/REEVAL_250X.md", "w").write("\n".join(lines) + "\n"); print("\n".join(lines))
