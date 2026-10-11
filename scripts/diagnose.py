"""Why is everything rejected?  Reproducible root-cause report -> docs/DIAGNOSIS.md  (reads development-stage metrics + aggregate final-stage outcomes; never feeds selection)."""
import json, sys, time, collections
import numpy as np
from pathlib import Path
from strategylab.db import DB

d = DB(); DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 4
cut = time.time() - DAYS * 86400
out = [f"# Diagnosis — why strategies are rejected (generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}, last {DAYS:g} days)\n"]

rows = d.q("SELECT stage,status,failure_reasons FROM experiments WHERE synthetic=0 AND finished>? AND timeframe!='4h'", (cut,))
st = collections.Counter((r["stage"], r["status"]) for r in rows)
out.append("## 1. Funnel (non-4h, real data)\n\n| stage | pass | fail |\n|---|---|---|")
for s in ("eligibility", "screen", "validate", "final"):
    out.append(f"| {s} | {st[(s,'pass')]} | {st[(s,'fail')]} |")
for s in ("eligibility", "screen", "validate", "final"):
    c = collections.Counter()
    for r in rows:
        if r["stage"] == s and r["status"] == "fail":
            for x in json.loads(r["failure_reasons"] or "[]"): c[x] += 1
    out.append(f"\n**{s} failures:** " + ", ".join(f"{k} {v}" for k, v in c.most_common(7)))

out.append("\n## 2. Gross edge vs cost per timeframe (screen stage, train data)\n\n| tf | screens | median gross R/trade | median net R/trade | median cost (R) | gross>0 | net>0 | net>0.05R | t>2 | trades/yr |\n|---|---|---|---|---|---|---|---|---|---|")
for tf in ("5m", "15m", "1h"):
    ms = [json.loads(x["metrics_json"]) for x in d.q("SELECT metrics_json FROM experiments WHERE stage='screen' AND synthetic=0 AND timeframe=? AND finished>? LIMIT 5000", (tf, cut))]
    ms = [m for m in ms if m.get("gross_mean_R") is not None and m.get("avg_stop")]
    if not ms: continue
    g = np.array([m["gross_mean_R"] for m in ms]); n = np.array([m["mean_R"] for m in ms]); cr = np.array([m["avg_cost"] / m["avg_stop"] for m in ms])
    t = np.array([m["t"] for m in ms]); tp = np.array([m.get("trades_per_year", 0) for m in ms])
    out.append(f"| {tf} | {len(ms)} | {np.median(g):.3f} | {np.median(n):.3f} | {np.median(cr):.3f} | {(g>0).mean():.0%} | {(n>0).mean():.0%} | {(n>0.05).mean():.1%} | {(t>2).mean():.1%} | {np.median(tp):.0f} |")

out.append("\n## 3. Candidates that reach the locked-test stage (non-4h)\n")
fin = [json.loads(r["metrics_json"]) for r in d.q("SELECT metrics_json FROM experiments WHERE stage='final' AND synthetic=0 AND timeframe!='4h' AND finished>?", (cut,))]
T = [(m["test"]["n"], m["test"]["mean_R"], m["test"]["t"], m.get("capital_test", {}).get("median_trades_per_month", 0),
      (m.get("capital_test", {}).get("final_wealth_pct") or {}).get("50", 0)) for m in fin if (m.get("test") or {}).get("n")]
if T:
    a = np.array(T)
    out.append(f"- {len(T)} candidates; median locked-test mean R/trade **{np.median(a[:,1]):.3f}**, positive in {(a[:,1]>0).mean():.0%}, t>2 in {(a[:,2]>2).mean():.1%}; median trades/month {np.median(a[:,3]):.1f}")
    best_r = np.median(a[:, 1]); tpm = np.median(a[:, 3])
    out.append(f"- arithmetic: at {tpm:.1f} trades/month and {best_r:.3f} R/trade, risking 5% per trade yields ≈ {tpm*best_r*5:.1f}% per month; the target needs ≈ 61% per month.")

# positioning-data rules vs the rest (aggregate diagnostic only; never used for selection)
P = ("oichg", "lstop", "lsacct", "tkls"); pos, oth = [], []
for r in d.q("SELECT s.spec_json sj, e.metrics_json m FROM experiments e JOIN strategies s ON s.id=e.strategy_id WHERE e.stage='final' AND e.synthetic=0 AND e.timeframe!='4h' AND e.finished>?", (cut,)):
    try:
        sp = json.loads(r["sj"]); t = (json.loads(r["m"]).get("test") or {})
        if not t.get("n"): continue
        isp = sp["family"] == "gp_rule" and any(a[0].startswith(P) for a in sp["params"]["rule"])
        (pos if isp else oth).append(t["mean_R"])
    except Exception: continue
if pos:
    out.append(f"\n### Positioning-data rules (open interest / long-short ratios) vs everything else, locked-test mean R/trade\n- positioning rules: n={len(pos)}, median {np.median(pos):.3f}, share>0 {np.mean(np.array(pos)>0):.0%}; other: n={len(oth)}, median {np.median(oth):.3f}, share>0 {np.mean(np.array(oth)>0):.0%}\n- reading: higher dev/validation pass rates for positioning rules did NOT carry to the locked test (edge ~0, |t|<2) -- the positioning history only starts in 2021 and its relationships are regime-specific.")
out.append("""
## 4. Root causes (ranked)
1. **No gross edge on short timeframes.** Median gross R/trade on 5m/15m is negative; the entry ideas (price/volume/flow/funding rules from public data) are not predictive at that horizon, and round-trip cost (0.10–0.16 R per trade) makes it worse. Searching more of the same on 5m is wasted compute — the director now suppresses cells whose recent screens show no gross edge.
2. **1h has a small positive edge (≈0.02–0.08 R/trade, mostly trend/drift) — far too small for the target.** Roughly 100 trades/yr × ~0.05 R × 5% risk ≈ 3% of equity per year before leverage; the target is +61%/month. The gap is more than an order of magnitude, and scaling by leverage multiplies drawdown/ruin first.
3. **Almost nothing survives out-of-sample.** Walk-forward/OOS failures and multiple-testing (Sidak over tens of thousands of trials) eliminate the apparent 1h winners; no candidate has t>2 on the locked test.
4. **Sample is two assets.** BTC and ETH are highly correlated, so the effective number of independent trade opportunities is small. Breadth (more perpetual markets) is the one lever that raises both trade frequency and diversification without raising per-trade risk.
5. **Duplicate/overfit churn** (already fixed in Phase 3b): exit-only variants and neighbouring parameters are now rejected before backtesting.

## 5. What works / what does not (for reference)
- WORKS: pipeline integrity (null-calibration, look-ahead test, placebo), honest cost model, crash-safe multi-worker service, pre-test filters, director + registry, GP grammar.
- DOES NOT: any 5m/15m rule family tried so far (negative gross edge); parameter-tuned 1h trend rules (decay OOS); portfolios of non-qualifying components (no diversification benefit large enough).
- UNPROVEN: that any public-data rule reaches +61%/month honestly. The system keeps searching (new markets, regime-conditional rules, GP) without relaxing gates.
""")
Path("docs/DIAGNOSIS.md").write_text("\n".join(out)); print("\n".join(out)[:3000])
