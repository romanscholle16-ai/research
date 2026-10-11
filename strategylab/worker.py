"""Research worker process: claims jobs from the persistent queue and executes them. Run via service.py."""
from __future__ import annotations
import json, os, sys, time, traceback
from .config import Settings, home_dir
from .db import DB
from .ai import Router
from .engine import DataStore, run_experiment
from .agents import Supervisor, NoData
from .director import Director
from . import data as D, resources
from .btcc import get_model

EXIT_RSS = 75


def make_loader():
    if os.environ.get("STRATEGYLAB_SYNTHETIC"):
        def loader(symbol, tf):
            seed = abs(hash(symbol)) % 1000 if False else sum(map(ord, symbol)) + {"15m": 1, "1h": 2, "4h": 3}.get(tf, 0)
            return D.synthetic(symbol, tf, n=int(os.environ.get("STRATEGYLAB_SYN_N", "6000")), seed=seed, with_edge=False)
        return loader
    return D.load_real


def faults():
    try: return json.loads(os.environ.get("STRATEGYLAB_FAULTS", "{}"))
    except Exception: return {}


def maybe_inject(db, kind):
    f = faults()
    if not f: return
    n = db.get("fault_counter", 0) + 1; db.set("fault_counter", n)
    done = db.get("faults_done", [])
    for name in ("crash", "hang", "error"):
        if n in f.get(name, []) and f"{name}{n}" not in done:
            db.set("faults_done", done + [f"{name}{n}"]); db.log("warn", "fault-injection", f"injecting {name} at job #{n}")
            if name == "crash": os._exit(1)
            if name == "hang": time.sleep(10_000)
            if name == "error": raise RuntimeError("injected failure")


def main():
    st = Settings.load(); db = DB()
    router = Router(db, st)
    if os.environ.get("STRATEGYLAB_AI_MOCK"):
        from .mockai import MockProvider
        router.providers = [MockProvider(db)]; router.st.ai_provider_order = ["mock"]
    store = DataStore(st, make_loader())
    em = get_model(st.exchange_preset)
    sup = Supervisor(db, st, router, store)
    director = Director(db, st, store, em)
    from . import audit as _audit
    try:
        if db.worker_id == 0: _audit.backfill_registry(db)
    except Exception as e: db.log('warn', 'worker', f'registry backfill failed: {e}')
    db.log("info", "worker", f"worker started pid={os.getpid()}")
    wid = db.worker_id; primary = wid == 0
    db.set(db.wkey("worker_pid"), os.getpid())
    my_kinds = None if primary else ("experiment", "recheck", "plan")
    stop = False
    while not stop:
        db.heartbeat()
        ctl = db.get("control", "stopped")
        if ctl == "stopped": break
        if not primary and wid >= int(Settings.load().n_workers): break      # scaled down
        if ctl == "paused": time.sleep(1); continue
        if resources.rss_mb() > st.max_rss_mb:
            db.log("warn", "resources", f"RSS {resources.rss_mb():.0f}MB over limit; restarting worker"); sys.exit(EXIT_RSS)
        if resources.free_mem_mb() < 800:
            db.set("blocker", "low_system_memory"); time.sleep(10); continue
        try:
            nw = max(1, int(Settings.load().n_workers))
            qd = db.q("SELECT SUM(kind='experiment' AND status='queued') e, SUM(kind='plan') p FROM jobs WHERE status IN ('queued','running') AND kind IN ('experiment','plan')")[0]
            if (qd["e"] or 0) < 30 * nw and (qd["p"] or 0) < nw: db.enqueue("plan", {"n": 24}, priority=1)   # keep every worker fed; planning runs on any worker
            loop_n = db.get(db.wkey("loop_n"), 0) + 1; db.set(db.wkey("loop_n"), loop_n)
            job = db.claim_job(kinds=my_kinds, exclude=("recheck",) if loop_n % 3 else ())  # rechecks get 1/3 of the slots
            if job is None:
                time.sleep(st.idle_sleep_s); continue
            sp_ = job["payload"].get("spec") or {}
            cj_ = {"id": job["id"], "kind": job["kind"], "since": time.time(), "note": job["payload"].get("note", ""),
                   "symbol": job["payload"].get("symbol"), "timeframe": job["payload"].get("timeframe"), "stage": "starting",
                   "family": sp_.get("family"), "params": sp_.get("params"), "side": sp_.get("side"), "stop_atr": sp_.get("stop_atr"),
                   "target_rr": sp_.get("target_rr"), "max_hold": sp_.get("max_hold"), "filters": sp_.get("filters")}
            db.heartbeat(job["id"]); db.set(db.wkey("current_job"), cj_)
            def stage_cb(name, cj_=cj_):
                cj_["stage"] = name; cj_["stage_since"] = time.time(); db.set(db.wkey("current_job"), cj_)
            try:
                maybe_inject(db, job["kind"])
                if job["kind"] == "plan":
                    ai_ok = db.get("ai_enabled", True)
                    if primary: sup.maybe_queue_portfolio(); sup.maybe_queue_forward()
                    plans = director.plan_batch(job["payload"].get("n", 24)) if (st.use_director and not os.environ.get("STRATEGYLAB_AI_MOCK")) else sup.plan(job["payload"].get("n", 8), use_ai=ai_ok)
                    for p in plans: db.enqueue("experiment", p, priority=5)
                    db.finish_job(job["id"], {"planned": len(plans)})
                    db.set("blocker", None)
                    if not plans:
                        time.sleep(1)
                elif job["kind"] == "data_sync":
                    n = sum(D.sync_daily(sym, st.research_end) for sym in st.symbols); db.set("data_sync_ts", time.time())
                    store.c.clear()           # reload frames so forward bars become visible
                    db.finish_job(job["id"], {"new_days": n}); db.log("info", "data", f"daily sync: {n} new day file(s)")
                elif job["kind"] == "forward":
                    from .forward import evaluate_forward
                    p_ = job["payload"]; db.finish_job(job["id"], evaluate_forward(db, st, store, p_["strategy_id"], p_["symbol"], p_["timeframe"], em))
                elif job["kind"] == "recheck":
                    from .engine import recheck_placebo
                    db.finish_job(job["id"], recheck_placebo(db, st, store, job["payload"]))
                elif job["kind"] == "portfolio":
                    from .portfolio import run_portfolio
                    res = run_portfolio(db, st, store, hb=lambda: db.heartbeat(job["id"])); db.finish_job(job["id"], res)
                    db.log("info", "portfolio", f"portfolio build: {res}")
                elif job["kind"] == "experiment":
                    res = run_experiment(db, st, store, job["payload"], hb=lambda: db.heartbeat(job["id"]), em=em, stage_cb=stage_cb)
                    db.finish_job(job["id"], res)
                    db.x("INSERT INTO kv(k,v) VALUES('cycles','1') ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+1")
                    db.set("last_experiment", {**res, "symbol": job["payload"]["symbol"], "timeframe": job["payload"]["timeframe"],
                                               "family": job["payload"]["spec"]["family"], "ts": time.time()})
                else:
                    db.fail_job(job["id"], "unknown job kind", max_attempts=1)
            except NoData:
                db.fail_job(job["id"], "no data", max_attempts=10**6, base_backoff=30); db.set("blocker", "no_data_available")
                db.log("error", "worker", "required historical data unavailable; waiting and rechecking"); time.sleep(5)
            except Exception as e:
                db.log("error", "worker", f"job {job['id']} failed: {e}\n{traceback.format_exc()[-600:]}")
                db.fail_job(job["id"], repr(e), max_attempts=st.max_job_attempts)
            finally:
                db.set(db.wkey("current_job"), None); db.heartbeat()
        except Exception as e:  # DB busy etc. - never die on a transient error
            db.log("error", "worker", f"loop error: {e}")
            try:   # never leave a claimed job stuck in 'running'
                for r in db.q("SELECT id FROM jobs WHERE status='running'"): db.fail_job(r["id"], f"loop error: {e}", max_attempts=st.max_job_attempts)
            except Exception: pass
            time.sleep(2)
    db.log("info", "worker", "worker exiting cleanly")


if __name__ == "__main__":
    main()
