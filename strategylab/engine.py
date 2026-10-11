"""Experiment execution: Backtesting Engineer + Statistical Validator + Risk Specialist agents (deterministic code)."""
from __future__ import annotations
import json, time
from collections import OrderedDict
import numpy as np
from . import strategies as ST, validation as V, data as D
from .backtest import period_bounds, metrics, Trades
from .btcc import DEFAULT
from .features import Features


class DataStore:
    """LRU cache of (df, meta, Features, bounds). `loader(symbol, tf)` -> (df, meta). Pluggable for tests (synthetic)."""
    def __init__(self, st, loader=None, max_items=4):
        self.st, self.loader, self.max = st, loader or D.load_real, max_items
        self.c = OrderedDict()

    def get(self, symbol, tf):
        k = (symbol, tf)
        if k in self.c: self.c.move_to_end(k); return self.c[k]
        df, meta = self.loader(symbol, tf)
        ref = None
        other = "ETHUSDT" if symbol == "BTCUSDT" else "BTCUSDT" if symbol == "ETHUSDT" else None
        if other and other in self.st.symbols:
            try: ref = self.loader(other, tf)[0]
            except Exception: ref = None
        F = Features(df, ref)
        item = dict(df=df, meta=meta, F=F, bounds=period_bounds(len(df), self.st.train_frac, self.st.val_frac), cache={})
        self.c[k] = item
        while len(self.c) > self.max: self.c.popitem(last=False)
        return item

    def available(self):
        out = []
        for s in self.st.symbols:
            for tf in self.st.timeframes:
                try: self.get(s, tf); out.append((s, tf))
                except Exception: pass
        return out


def _record_dataset(db, meta):
    db.x("INSERT OR IGNORE INTO datasets(id,symbol,timeframe,source,start_ts,end_ts,rows,gaps,has_funding,has_taker,synthetic,notes,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (meta["id"], meta["symbol"], meta["timeframe"], meta["source"], meta["start_ts"], meta["end_ts"], meta["rows"], meta["gaps"],
          meta["has_funding"], meta["has_taker"], meta["synthetic"], meta["notes"], time.time()))


def run_experiment(db, st, store: DataStore, payload: dict, hb=lambda: None, em=DEFAULT) -> dict:
    spec, symbol, tf = payload["spec"], payload["symbol"], payload["timeframe"]
    errs = ST.validate_spec(spec)
    if errs: raise ValueError("invalid spec: " + "; ".join(errs))
    sid = "S-" + ST.spec_hash(spec)[:10]
    item = store.get(symbol, tf); meta = item["meta"]; F, bounds = item["F"], item["bounds"]
    _record_dataset(db, meta)
    syn = int(meta["synthetic"])
    done = db.q("SELECT 1 FROM strategy_status WHERE strategy_id=? AND symbol=? AND timeframe=?", (sid, symbol, tf))
    if done: return {"skipped": "already_tested", "strategy_id": sid}
    req = ST.FAMILIES[spec["family"]]["requires"]
    if ("funding" in req and not meta["has_funding"]) or ("ref" in req and F.ref is None):
        return {"skipped": "missing_data", "strategy_id": sid}
    db.x("INSERT OR IGNORE INTO strategies(id,created,hypothesis_id,parent_id,family,spec_json,spec_hash) VALUES(?,?,?,?,?,?,?)",
         (sid, time.time(), payload.get("hypothesis_id"), payload.get("parent_id"), spec["family"], json.dumps(spec), ST.spec_hash(spec)))
    rng = np.random.default_rng(int(ST.spec_hash(spec), 16) % 2**32)
    stage_reasons = {}; summary = {"dataset": meta["id"], "synthetic": bool(syn)}
    t_start = time.time()
    min_trades = 40 if tf == "4h" else 60

    def record(stage, status, metrics_, reasons):
        db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,dataset_id,started,finished,status,metrics_json,failure_reasons,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (sid, stage, symbol, tf, meta["id"], t_start, time.time(), status, json.dumps(metrics_, default=float), json.dumps(reasons), syn))

    def finish(status, reasons):
        summary["reasons"] = reasons
        db.x("INSERT OR REPLACE INTO strategy_status(strategy_id,symbol,timeframe,status,updated,summary_json,synthetic) VALUES(?,?,?,?,?,?,?)",
             (sid, symbol, tf, status, time.time(), json.dumps(summary, default=float), syn))
        if status == "REJECTED":
            db.x("INSERT INTO lessons(ts,family,symbol,timeframe,reasons,strategy_id) VALUES(?,?,?,?,?,?)",
                 (time.time(), spec["family"], symbol, tf, json.dumps(reasons), sid))
        return {"strategy_id": sid, "status": status, "reasons": reasons}

    tr, m, rs = V.screen(F, spec, bounds, em, min_trades); hb()
    summary["screen"] = m; record("screen", "fail" if rs else "pass", m, rs)
    if rs: return finish("REJECTED", rs)
    stage_reasons["screen"] = rs
    out, rs, trv = V.validate(F, spec, bounds, em, st, tr, rng); hb()
    summary["validate"] = out; record("validate", "fail" if rs else "pass", out, rs)
    if rs: return finish("REJECTED", rs)
    trtv = Trades.concat([tr, trv])
    n_tested = db.q("SELECT COUNT(*) n FROM strategies")[0]["n"]
    locked = db.q("SELECT 1 FROM experiments WHERE strategy_id=? AND symbol=? AND timeframe=? AND stage='final'", (sid, symbol, tf))
    if locked: return finish("PROMISING", ["test_already_consumed"])
    out, rs, test = V.final_stage(F, spec, bounds, em, st, symbol, n_tested, trtv); hb()
    summary["final"] = out; record("final", "fail" if rs else "pass", {k: v for k, v in out.items() if k != "capital_all"}, rs)
    allt = Trades.concat([trtv, test]) if test is not None else trtv
    db.x("INSERT OR REPLACE INTO trades VALUES(?,?,?,?,?)", (sid, symbol, tf, "all", json.dumps(allt.to_json())))
    stage_reasons["final"] = rs
    status = V.qualification_status(stage_reasons)
    if status == "BACKTEST_QUALIFIED": db.log("info", "qualify", f"{sid} {symbol} {tf} BACKTEST_QUALIFIED (synthetic={bool(syn)})")
    return finish(status, rs)
