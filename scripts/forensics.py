"""Forensic audit of the experiment history -> docs/FORENSICS.md. Read-only; every number is a SQL/Python count over the live DB."""
import json, sys, time, collections, os
from strategylab.db import DB
from strategylab import strategies as ST, feasibility as FE

db = DB(); out = []; P = out.append
q = db.q
tot = q("SELECT COUNT(*) n, COUNT(DISTINCT strategy_id) s FROM experiments WHERE synthetic=0")[0]
P(f"# Forensic audit ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})\n")
P(f"Experiment rows: **{tot['n']}** over **{tot['s']}** distinct strategy specs.\n")
# --- uniqueness of hypotheses (entry logic) vs exit-only variants
ent, spec_n, fam_ent = collections.Counter(), 0, collections.defaultdict(set)
rows = q("SELECT s.id, s.family, s.spec_json, ss.symbol, ss.timeframe FROM strategies s JOIN strategy_status ss ON ss.strategy_id=s.id WHERE ss.synthetic=0")
for r in rows:
    try: sp = json.loads(r["spec_json"]); k = (ST.entry_key(sp), r["symbol"], r["timeframe"])
    except Exception: continue
    ent[k] += 1; fam_ent[r["family"]].add(k); spec_n += 1
uniq = len(ent); multi = sum(1 for v in ent.values() if v > 1); extra = sum(v - 1 for v in ent.values() if v > 1)
P("## 1. How many experiments were genuinely unique?")
P(f"- strategy x market rows analysed: {spec_n}; distinct (entry logic, symbol, timeframe) hypotheses: **{uniq}** ({uniq/max(spec_n,1):.1%}).")
P(f"- {multi} hypotheses were tested with >1 exit variant; {extra} rows ({extra/max(spec_n,1):.1%}) are exit/risk-only re-tests of an entry already tried.")
top = sorted(((len(v), f) for f, v in fam_ent.items()), reverse=True)
P("- distinct hypotheses per family: " + ", ".join(f"{f}={n}" for n, f in top[:14]))
# --- funnel
P("\n## 2. Where experiments end (stage / status)")
for r in q("SELECT stage,status,COUNT(*) n FROM experiments WHERE synthetic=0 GROUP BY 1,2 ORDER BY n DESC LIMIT 14"): P(f"- {r['stage']}/{r['status']}: {r['n']}")
# --- reasons by class
cls = collections.Counter(); rs = collections.Counter()
for r in q("SELECT failure_reasons fr FROM experiments WHERE synthetic=0 AND status='fail'"):
    try: lst = json.loads(r["fr"] or "[]")
    except Exception: continue
    for x in lst: rs[x] += 1
    cls[FE.classify(lst) or "none"] += 1
P("\n## 3. Why they failed (rows can carry several reasons)")
tot_f = sum(cls.values())
P("- primary class: " + ", ".join(f"{k}={v} ({v/tot_f:.0%})" for k, v in cls.most_common()))
P("- top reasons: " + ", ".join(f"{k}={v}" for k, v in rs.most_common(14)))
fee = sum(v for k, v in rs.items() if k in ("cost_infeasible", "cost_dominated", "cost_sensitive", "cost_infeasible_precheck", "not_leverage_capable"))
P(f"- fee/slippage-related reason occurrences: **{fee}**")
pre = q("SELECT reason, COUNT(*) n FROM precheck_rejections GROUP BY 1 ORDER BY n DESC")
P("\n## 4. Rejected before any signal computation (director precheck)")
P("- " + ", ".join(f"{r['reason']}={r['n']}" for r in pre))
# --- repeats / learning
P("\n## 5. Does the director learn?")
rep = q("SELECT COUNT(*) n FROM precheck_rejections WHERE reason IN ('exit_variant_limit','noise_neighbourhood','duplicate')")[0]["n"]
P(f"- candidates blocked as repeats/near-duplicates by the registry: {rep}")
dbat = q("SELECT COUNT(*) n, SUM(json_extract(composition_json,'$.rejected_precheck')) r FROM director_batches")[0]
P(f"- director batches: {dbat['n']}; candidates dropped pre-queue: {dbat['r']}")
st = db.get("director_state", {}) or {}
P(f"- director state: mode={st.get('mode')}, best dev-validated t={st.get('best')}, experiments since improvement={st.get('exp',0)-st.get('exp_last_improve',0)}")
# --- data quality
P("\n## 6. Data availability per cell (skipped for missing data)")
md = q("SELECT symbol, timeframe, COUNT(*) n FROM experiments WHERE synthetic=0 AND failure_reasons LIKE '%missing_data%' GROUP BY 1,2")
P("- " + (", ".join(f"{r['symbol']}/{r['timeframe']}={r['n']}" for r in md) or "none recorded"))
# --- survivors
P("\n## 7. What survived")
for r in q("SELECT stage,status,COUNT(*) n FROM experiments WHERE synthetic=0 AND stage IN ('validate','final','recheck_placebo') GROUP BY 1,2"): P(f"- {r['stage']}/{r['status']}: {r['n']}")
fr = collections.Counter()
for r in q("SELECT failure_reasons fr FROM experiments WHERE stage='final' AND status='fail'"):
    for x in json.loads(r["fr"] or "[]"): fr[x] += 1
P("- locked-test failures by reason: " + ", ".join(f"{k}={v}" for k, v in fr.most_common(8)))
open(os.path.join(os.path.dirname(__file__), "..", "docs", "FORENSICS.md"), "w").write("\n".join(out) + "\n")
print("\n".join(out))
