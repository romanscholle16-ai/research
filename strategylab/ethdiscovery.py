"""Orchestration of the ETH opportunity-discovery engine: census -> train-only discovery -> embargoed validation -> persist -> queue into the funnel."""
from __future__ import annotations
import json, math, time
from dataclasses import replace
import numpy as np
from . import opportunity as OP, strategies as ST

# (leverage the stop is sized for, stop as fraction of price).  500x is arithmetically infeasible with an assumed 0.1% maintenance margin (liq 0.10% < min stop 0.10%).
LEV_STOPS = [(250, 0.0020), (100, 0.0050), (50, 0.0100), (25, 0.0200)]
RRS = (2.0, 3.0, 4.0)
TF_H = {"15m": 16, "5m": 36}
SIDES = ("long", "short")

SCHEMA = """CREATE TABLE IF NOT EXISTS eth_patterns(
  id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, timeframe TEXT, side TEXT, lev INTEGER, stop_pct REAL, rr REAL, horizon INTEGER,
  conds_json TEXT, train_json TEXT, val_json TEXT, status TEXT, spec_json TEXT, adj_p REAL);
CREATE TABLE IF NOT EXISTS eth_census(id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, timeframe TEXT, lev INTEGER, report_json TEXT);"""


def ensure_schema(db):
    for stmt in SCHEMA.split(";"):
        if stmt.strip(): db.x(stmt)


def _model(em, lev):
    return replace(em, min_lev_capable=lev, max_leverage=max(em.max_leverage, lev), high_leverage=True)


def sidak(p, k): return 1 - (1 - min(max(p, 0.0), 1.0)) ** max(k, 1)


def norm_sf(z): return 0.5 * math.erfc(z / math.sqrt(2))


def run_all(db, store, em, symbol="ETHUSDT", tfs=("15m", "5m"), budget_s=1500, log=lambda m: None, queue=True, max_queue=24):
    ensure_schema(db); t0 = time.time(); stats = dict(configs=0, finalists=0, validated=0, queued=0, tested_hypotheses=0)
    found = []
    for tf in tfs:
        it = store.get(symbol, tf); F, bounds = it["F"], it["bounds"]; H = TF_H[tf]
        for lev, sf in LEV_STOPS:
            m = _model(em, lev)
            census = OP.study(F, tf, m, *bounds["train"], H, s_frac=sf, rrs=RRS)
            db.x("INSERT INTO eth_census(ts,symbol,timeframe,lev,report_json) VALUES(?,?,?,?,?)", (time.time(), symbol, tf, lev, json.dumps(census)))
            for rr in RRS:
                for side in SIDES:
                    if time.time() - t0 > budget_s: break
                    fins, meta = OP.discover(F, tf, m, bounds, side, sf, rr, H)
                    stats["configs"] += 1; stats["tested_hypotheses"] += meta["tested"]; stats["finalists"] += len(fins)
                    k = max(len(fins), 1)
                    for f in fins:
                        val = OP.predict(F, tf, m, bounds, f, meta)
                        # one-sided p of mean R > 0 on validation, Sidak-corrected for the finalists of this config AND the configs searched
                        p = norm_sf(val["t"]) if val.get("t") is not None else 1.0
                        adj = sidak(p, k * 12)
                        ok = bool(val.get("mean_R") is not None and val["n_val"] >= 40 and val["mean_R"] > 0 and val["t"] >= 1.28)
                        spec = OP.to_spec(f, meta, it["F"].atr_frac_med(*bounds["train"], 14))
                        status = "validated" if ok else "rejected_on_validation"
                        db.x("INSERT INTO eth_patterns(ts,symbol,timeframe,side,lev,stop_pct,rr,horizon,conds_json,train_json,val_json,status,spec_json,adj_p) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (time.time(), symbol, tf, side, lev, 100 * sf, rr, H, json.dumps(f["conds"]), json.dumps(dict(f, meta=meta)), json.dumps(val), status, json.dumps(spec), adj))
                        if ok: stats["validated"] += 1; found.append((val["t"], tf, spec, f, val))
            log(f"{tf} lev{lev}: configs={stats['configs']} validated={stats['validated']} ({time.time() - t0:.0f}s)")
    if queue:
        found.sort(key=lambda x: -x[0])
        for t, tf, spec, f, val in found[:max_queue]:
            if ST.validate_spec(spec): continue
            db.enqueue("experiment", dict(spec=spec, symbol=symbol, timeframe=tf, note=f"opp:{spec['side']}:{'&'.join(c[0] for c in f['conds'])}"[:120]), priority=2); stats["queued"] += 1
    db.set("eth_discovery_last", time.time()); db.set("eth_discovery_stats", stats)
    return stats
