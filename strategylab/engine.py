"""Experiment execution: Backtesting Engineer + Statistical Validator + Risk Specialist agents (deterministic code)."""
from __future__ import annotations
import json, time
import pandas as pd
from collections import OrderedDict
import numpy as np
from . import strategies as ST, validation as V, data as D, feasibility as FE
from .backtest import period_bounds, metrics, Trades
from .btcc import DEFAULT
from .features import Features


class DataStore:
    """LRU cache of (df, meta, Features, bounds). `loader(symbol, tf)` -> (df, meta). Pluggable for tests (synthetic)."""
    def __init__(self, st, loader=None, max_items=8):
        self.st, self.loader, self.max = st, loader or D.load_real, max_items
        self.c = OrderedDict()

    def get(self, symbol, tf):
        k = (symbol, tf)
        if k in self.c: self.c.move_to_end(k); return self.c[k]
        df, meta = self.loader(symbol, tf)
        full = df
        cut = pd.Timestamp(self.st.research_end, tz="UTC") if getattr(self.st, "research_end", None) else None
        if cut is not None and not meta.get("synthetic"): df = df[df.index < cut]
        ref = None
        other = "ETHUSDT" if symbol == "BTCUSDT" else "BTCUSDT"      # BTC is the reference market for every other symbol (BTC itself uses ETH)
        if other and other in self.st.symbols:
            try:
                ref = self.loader(other, tf)[0]
                if cut is not None and not meta.get("synthetic"): ref = ref[ref.index < cut]
            except Exception: ref = None
        F = Features(df, ref)
        item = dict(df=df, df_full=full, meta=meta, F=F, bounds=period_bounds(len(df), self.st.train_frac, self.st.val_frac), cache={})
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


def run_experiment(db, st, store: DataStore, payload: dict, hb=lambda: None, em=DEFAULT, stage_cb=lambda name: None) -> dict:
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
    stage_reasons = {}; summary = {"dataset": meta["id"], "synthetic": bool(syn)}; fp_key = None
    t_start = time.time()
    min_trades = 40 if tf == "4h" else 60

    def record(stage, status, metrics_, reasons):
        db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,dataset_id,started,finished,status,metrics_json,failure_reasons,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (sid, stage, symbol, tf, meta["id"], t_start, time.time(), status, json.dumps(metrics_, default=float), json.dumps(reasons), syn))

    def finish(status, reasons):
        summary["reasons"] = reasons; summary["failure_class"] = FE.classify(reasons) if status == "REJECTED" else None
        stage_ = 3 if "final" in summary else 2 if "validate" in summary else 1 if "screen" in summary else 0   # 0 eligibility-fail, 1 screen-fail, 2 validate-fail, 3 validate-pass
        FE.register(db, spec, symbol, tf, stage_, status == "REJECTED")
        if fp_key:                                              # remember how often entries with this signal fingerprint failed
            db.x("INSERT INTO signal_fp(symbol,timeframe,fp,family,n_tested,n_failed) VALUES(?,?,?,?,1,?) ON CONFLICT(symbol,timeframe,fp) DO UPDATE SET n_tested=n_tested+1, n_failed=n_failed+?",
                 (symbol, tf, fp_key, spec["family"], int(status == "REJECTED"), int(status == "REJECTED")))
        db.x("INSERT OR REPLACE INTO strategy_status(strategy_id,symbol,timeframe,status,updated,summary_json,synthetic) VALUES(?,?,?,?,?,?,?)",
             (sid, symbol, tf, status, time.time(), json.dumps(summary, default=float), syn))
        if status == "REJECTED":
            db.x("INSERT INTO lessons(ts,family,symbol,timeframe,reasons,strategy_id) VALUES(?,?,?,?,?,?)",
                 (time.time(), spec["family"], symbol, tf, json.dumps(reasons), sid))
        return {"strategy_id": sid, "status": status, "reasons": reasons}

    # ---- Stage A: cheap eligibility + economic feasibility (no backtest, no optimisation)
    stage_cb("A eligibility")
    pre = {}                                                    # checks that need neither signals nor a backtest
    pc = FE.precheck_cost(F, spec, bounds, em); pre["cost"] = pc
    ok_n, why_n = FE.novelty_check(db, spec, symbol, tf)
    if not pc["ok"] or not ok_n:
        why = ["cost_infeasible_precheck"] if not pc["ok"] else [why_n]
        summary["eligibility"] = dict(decision="reject", reasons=why, failure_class=FE.classify(why), **pre, precheck=True)
        record("eligibility", "fail", summary["eligibility"], why)
        return finish("REJECTED", why)
    ek_ = ST.entry_key(spec); cache_ = item["cache"]
    if ek_ in cache_: sig = cache_[ek_]                          # entry signals are independent of exits: reuse across exit variants
    else:
        sig = ST.signals(F, spec); cache_[ek_] = sig
        while len(cache_) > 48: cache_.pop(next(iter(cache_)))
    lo_, hi_ = bounds["train"]
    ea = FE.stage_a(F, spec, bounds, em, st, sig); summary["eligibility"] = ea
    fp_key = FE.signal_fingerprint(F, spec, lo_, hi_, sig); ea["signal_fp"] = fp_key
    if ea["decision"] == "pass":
        dup = db.q("SELECT n_failed FROM signal_fp WHERE symbol=? AND timeframe=? AND fp=?", (symbol, tf, fp_key))
        if dup and dup[0]["n_failed"] >= 3:                     # same entries already failed 3 times => exit-only tweak of a failed hypothesis
            ea["decision"] = "reject"; ea["reasons"] = ["near_duplicate_failed_hypothesis"]; ea["failure_class"] = "duplicate"
    record("eligibility", "pass" if ea["decision"] == "pass" else "fail", ea, ea["reasons"])
    if ea["decision"] == "reject": return finish("REJECTED", ea["reasons"])
    stage_cb("B fast backtest")
    tr, m, rs = V.screen(F, spec, bounds, em, min_trades, {ST.spec_hash(spec): sig}); hb()
    summary["screen"] = m; record("screen", "fail" if rs else "pass", m, rs)
    if rs: return finish("REJECTED", rs)
    stage_reasons["screen"] = rs
    stage_cb("C robustness")
    out, rs, trv = V.validate(F, spec, bounds, em, st, tr, rng); hb()
    summary["validate"] = out; record("validate", "fail" if rs else "pass", out, rs)
    if rs: return finish("REJECTED", rs)
    trtv = Trades.concat([tr, trv])
    n_tested = db.q("SELECT COUNT(*) n FROM strategies")[0]["n"]
    locked = db.q("SELECT 1 FROM experiments WHERE strategy_id=? AND symbol=? AND timeframe=? AND stage='final'", (sid, symbol, tf))
    if locked: return finish("PROMISING", ["test_already_consumed"])
    stage_cb("D qualification")
    out, rs, test = V.final_stage(F, spec, bounds, em, st, symbol, n_tested, trtv); hb()
    summary["final"] = out; record("final", "fail" if rs else "pass", {k: v for k, v in out.items() if k != "capital_all"}, rs)
    allt = Trades.concat([trtv, test]) if test is not None else trtv
    db.x("INSERT OR REPLACE INTO trades VALUES(?,?,?,?,?)", (sid, symbol, tf, "all", json.dumps(allt.to_json())))
    stage_reasons["final"] = rs
    status = V.qualification_status(stage_reasons)
    if status == "BACKTEST_QUALIFIED": db.log("info", "qualify", f"{sid} {symbol} {tf} BACKTEST_QUALIFIED (synthetic={bool(syn)})")
    return finish(status, rs)


def recheck_placebo(db, st, store, payload, em=DEFAULT):
    """Apply the random-entry (beta-control) gate to a strategy that was promoted before the gate existed."""
    sid, symbol, tf = payload["strategy_id"], payload["symbol"], payload["timeframe"]
    r = db.q("SELECT ss.status, ss.summary_json sj, s.spec_json sp FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.strategy_id=? AND ss.symbol=? AND ss.timeframe=?", (sid, symbol, tf))
    if not r or r[0]["status"] == "REJECTED": return {"skipped": "n/a"}
    sm = json.loads(r[0]["sj"]); spec = json.loads(r[0]["sp"])
    if "placebo" in sm.get("validate", {}): return {"skipped": "already_checked"}
    item = store.get(symbol, tf); F, b = item["F"], item["bounds"]
    rng = np.random.default_rng(int(ST.spec_hash(spec), 16) % 2**32)
    pb = V.placebo_test(F, spec, em, b["train"][0], b["val"][1], rng)
    sm.setdefault("validate", {})["placebo"] = pb
    status = r[0]["status"]
    if pb["p"] > 0.15:
        status = "REJECTED"; sm["reasons"] = ["no_alpha_vs_random_entries"] + [x for x in sm.get("reasons", []) if x != "no_alpha_vs_random_entries"]
        db.x("INSERT INTO lessons(ts,family,symbol,timeframe,reasons,strategy_id) VALUES(?,?,?,?,?,?)", (time.time(), spec["family"], symbol, tf, json.dumps(["no_alpha_vs_random_entries"]), sid))
    db.x("UPDATE strategy_status SET status=?, summary_json=?, updated=? WHERE strategy_id=? AND symbol=? AND timeframe=?", (status, json.dumps(sm, default=float), time.time(), sid, symbol, tf))
    db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,dataset_id,started,finished,status,metrics_json,failure_reasons,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
         (sid, "recheck_placebo", symbol, tf, item["meta"]["id"], time.time(), time.time(), "fail" if status == "REJECTED" else "pass", json.dumps(pb), json.dumps([] if status != "REJECTED" else ["no_alpha_vs_random_entries"])))
    return {"strategy_id": sid, "status": status, "placebo_p": pb["p"]}
