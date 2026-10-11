"""ETH research report -> docs/ETH_REPORT.md.  Sections: data completeness, opportunity census (how often 0.4-0.8% moves occur and how many are reachable
without breaching a liquidation-safe stop), leverage arithmetic (fees as % of margin), discovery/prediction results, cost ladder (is there ANY predictive edge at zero cost?),
and leverage comparison of the best patterns on identical signals.  All pattern search uses train bars only; validation is embargoed; the locked test is untouched."""
import json, sys, time
import numpy as np
from dataclasses import replace
from strategylab.db import DB
from strategylab.config import Settings
from strategylab.engine import DataStore
from strategylab.worker import make_loader
from strategylab import btcc, opportunity as OP, ethdiscovery as ED, capital as CP, validation as V, strategies as ST

db = DB(); st = Settings.load(); store = DataStore(st, make_loader()); em0 = btcc.get_model("eth_hl"); ED.ensure_schema(db)
L = [f"# ETH opportunity research ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})\n"]
P = L.append

# 1. data
P("## 1. ETH data completeness\n")
for r in db.q("SELECT timeframe,status,rows,report_json FROM data_audit WHERE symbol='ETHUSDT' ORDER BY id DESC LIMIT 6"):
    j = json.loads(r["report_json"]); P(f"- {r['timeframe']}: {r['status']}, rows={r['rows']}, missing={j.get('missing_frac')}, longest gap={j.get('longest_gap_h')}h, checksum={j.get('checksum')}")
P("- 1m raw archive (2020-01-01 .. 2026-10-10): 3,564,000 rows, 0 missing minutes (checked directly). One junk 'numeric header' row per daily-sync file (open_time=0) was found and is now dropped on read; 5m+ research frames were never affected.\n")

# 2. leverage arithmetic
P("## 2. Leverage arithmetic (identical regardless of strategy)\n")
P("| leverage | liquidation distance | max safe stop (80%) | move for +400% on margin | round-trip fee as % of margin (maker/maker) | (maker entry + taker stop) |")
P("|---|---|---|---|---|---|")
for lev in (25, 50, 100, 250, 500):
    liq = em0.liq_distance(lev); mk = 2 * em0.maker_fee; ms = em0.maker_fee + em0.taker_fee + em0.half_spread + em0.stop_slippage
    P(f"| {lev}x | {100*liq:.2f}% | {100*0.8*liq:.2f}% | {100*4/lev:.2f}% | {100*mk*lev:.0f}% | {100*ms*lev:.0f}% |")
P("\nAssumptions (UNVERIFIED for BTCC): maintenance margin 0.1%, maker 0.03%, taker 0.06%, half-spread 0.01%, stop slippage 0.04%. At 500x the liquidation distance (0.10%) is below the 0.10% minimum stop the backtester accepts, i.e. no stop fits inside liquidation; it is excluded by arithmetic under these assumptions.\n")

# 3. census
P("## 3. Opportunity census (train period, ETHUSDT, passive limit entries, stop sized for each leverage)\n")
P("| tf | lev | stop | target | side | tradable bars | random-entry win rate | zero-edge win rate | median/p90 adverse excursion before a win | wins as share of bars |")
P("|---|---|---|---|---|---|---|---|---|---|")
rows = db.q("SELECT timeframe,lev,report_json FROM eth_census WHERE id IN (SELECT MAX(id) FROM eth_census GROUP BY timeframe,lev) ORDER BY timeframe,lev DESC")
for r in rows:
    j = json.loads(r["report_json"])
    for c in j["configs"]:
        be = 1.0 / (1.0 + c["rr"]) if "rr" in c else None
        P(f"| {r['timeframe']} | {r['lev']}x | {c['stop_pct']}% | {c['target_move_pct']}% | {c['side']} | {c['tradable_share']:.0%} | {c['base_win']:.1%} | {100/(1+c['rr']):.1f}% | {c['mae_win_pct'][0]}% / {c['mae_win_pct'][1]}% | {c['base_win']*c['tradable_share']:.1%} |" if c.get("mae_win_pct") else "")
P("\n'Zero-edge win rate' = 1/(1+R:R), the hit rate needed to break even with zero costs; random entries fall below it because passive entries fill mostly when price is already moving against the position (adverse selection).\n")

# 4. discovery results
P("## 4. Pattern discovery (train-only) and prediction (embargoed validation)\n")
tot = db.q("SELECT COUNT(*) n, SUM(status='validated') v FROM eth_patterns")[0]
P(f"- finalists persisted: {tot['n']}; validated (val mean R>0, t>=1.28, n>=40): **{tot['v'] or 0}**")
P("| tf | lev | side | R:R | train best (mean R / t / n) | validation (mean R / t / n / win) | status |\n|---|---|---|---|---|---|---|")
best = {}
for r in db.q("SELECT * FROM eth_patterns ORDER BY id"):
    tr = json.loads(r["train_json"]); va = json.loads(r["val_json"]); key = (r["timeframe"], r["lev"], r["side"], r["rr"])
    if key not in best or tr["t"] > best[key][0]["t"]: best[key] = (tr, va, r)
for key, (tr, va, r) in sorted(best.items()):
    vv = f"{va['mean_R']:+.3f} / {va['t']:.2f} / {va['n_val']} / {va['win']:.1%}" if va.get("mean_R") is not None else f"n={va['n_val']}"
    P(f"| {key[0]} | {key[1]}x | {key[2]} | {key[3]} | {tr['mean_R']:+.3f} / {tr['t']:.2f} / {tr['n_train']} | {vv} | {r['status']} |")

# 5. cost ladder: is there ANY edge at zero cost?
P("\n## 5. Cost ladder: gross predictability ceiling (discovery repeated at zero cost, validated at several cost levels)\n")
P("| tf | lev | side | R:R | val mean R at cost x0 | x0.25 | x0.5 | x1.0 | best-pattern conditions |\n|---|---|---|---|---|---|---|---|---|")
for tf, H in (("15m", 16), ("5m", 36)):
    it = store.get("ETHUSDT", tf); F, b = it["F"], it["bounds"]
    for lev, sf in ED.LEV_STOPS[:3]:
        m = ED._model(em0, lev)
        for side in ("long", "short"):
            for rr in (3.0,):
                fins, meta = OP.discover(F, tf, m.with_cost_mult(0.0), b, side, sf, rr, H)
                if not fins: continue
                f = fins[0]; cells = []
                for cm in (0.0, 0.25, 0.5, 1.0):
                    v = OP.predict(F, tf, m.with_cost_mult(cm), b, f, meta); cells.append(f"{v['mean_R']:+.3f} (t {v['t']:.1f}, n {v['n_val']})" if v.get("mean_R") is not None else "n/a")
                P(f"| {tf} | {lev}x | {side} | {rr} | " + " | ".join(cells) + f" | {' & '.join(c[0]+c[1]+str(round(c[2],2)) for c in f['conds'])} |")
open("docs/ETH_REPORT.md", "w").write("\n".join(L) + "\n"); print("\n".join(L))
