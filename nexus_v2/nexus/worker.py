"""Research worker process: claims durable jobs, heartbeats from a side thread, exits gracefully on stop."""
import json, os, signal, sys, threading, time, traceback

from . import config, data, db, jobs, log, research as R, runtime as runtime_mod, strategies as S, sysinfo, validation as V

LG = log.get("worker")
STOP = threading.Event()


def _sig(*_):
    STOP.set()


def h_experiment(conn, job, p, progress):
    spec = p["spec"]
    t0 = time.perf_counter()
    r = R.evaluate(conn, spec)
    ms = (time.perf_counter() - t0) * 1000
    if r["status"] == "data_failure":
        raise RuntimeError("data_failure: " + r["reason"])          # infrastructure/data class: retried, not recorded as research
    sid, new = R.persist(conn, spec, r, p["agent"], p.get("hypothesis_id"), p.get("why", ""), job["id"], runtime_ms=ms)
    return {"strategy_id": sid, "new": new, "status": r["status"]}


def h_validate(conn, job, p, progress):
    return {"notes": V.run_validation(conn, p["strategy_id"], progress)}


def h_challenge(conn, job, p, progress):
    return V.adversarial_test(conn, p["strategy_id"])


def h_paper_forward(conn, job, p, progress):
    done = 0
    for r in conn.execute("SELECT strategy_id FROM validations WHERE stage='holdout' AND passed=1").fetchall():
        progress(f"paper-forward {r[0]}")
        V.refresh_paper_forward(conn, r[0])
        done += 1
    return {"candidates": done}


def h_data_update(conn, job, p, progress):
    out = {}
    for sym in config.DATA["symbols"]:
        progress(f"sync {sym}")
        out[sym] = data.sync_symbol(conn, sym)
        if out[sym].get("failed", 0) and not out[sym].get("ok", 0):
            raise RuntimeError(f"data sync failed for {sym}: {out[sym]}")
        if out[sym].get("ok", 0) or not conn.execute("SELECT 1 FROM datasets WHERE symbol=?", (sym,)).fetchone():
            data.build_dataset(conn, sym)
    return out


HANDLERS = {"experiment": h_experiment, "validate": h_validate, "challenge": h_challenge, "paper_forward": h_paper_forward,
            "data_update": h_data_update}


def run(name, once=False, max_jobs=None):
    STOP.clear()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sig)
    conn = db.open_db()
    hb_conn = db.connect()
    pid = os.getpid()
    cur = {"id": None}
    n_done = 0

    def beat():
        while not STOP.is_set():
            try:
                hb_conn.execute("UPDATE processes SET heartbeat=?, pid=? WHERE name=?", (time.time(), pid, name))
                if cur["id"]:
                    jobs.heartbeat(hb_conn, cur["id"])
            except Exception:
                pass
            STOP.wait(5.0)
    threading.Thread(target=beat, daemon=True).start()
    LG.info("worker %s started pid=%s", name, pid)
    idle = 0
    while not STOP.is_set():
        if runtime_mod.child_should_exit(conn):
            LG.warning("superseded by a newer supervisor (or supervisor gone); exiting")
            break
        desired = db.get_state(conn, "desired", "stopped")
        throttle = db.get_state(conn, "throttle", "none")
        if desired != "running" or throttle == "high" or db.get_state(conn, f"worker_stop:{name}") == "1":
            if desired == "stopped" or db.get_state(conn, f"worker_stop:{name}") == "1":
                break
            STOP.wait(2.0)
            continue
        if sysinfo.proc_rss_mb(pid) > 1500:
            LG.warning("worker RSS above 1500MB; exiting for a clean restart")
            break
        job = jobs.claim(conn, name, pid)
        if job is None:
            idle += 1
            if once and idle > 2:
                break
            STOP.wait(0.5 if idle < 20 else 2.0)
            continue
        idle = 0
        cur["id"] = job["id"]
        p = json.loads(job["payload"])
        try:
            res = HANDLERS[job["type"]](conn, job, p, lambda msg, j=job["id"]: jobs.heartbeat(conn, j, msg))
            jobs.complete(conn, job["id"], res)
        except Exception as e:
            tb = traceback.format_exc()
            LG.error("job %s (%s) failed: %s", job["id"], job["type"], tb[-1500:])
            outcome = jobs.fail(conn, job["id"], f"{type(e).__name__}: {e}")
            comp = "data" if "data" in str(e).lower() else "worker"
            with db.tx(conn):
                db.incident(conn, comp, "warning" if outcome == "retry" else "error", f"job {job['type']}#{job['id']}: {type(e).__name__}: {e}",
                            likely_cause="exception in job handler", recovery=f"job {outcome} (attempt {job['attempts']}/{job['max_attempts']})",
                            state={"job": job["id"], "type": job["type"]}, fingerprint=f"job:{job['type']}:{type(e).__name__}:{str(e)[:50]}")
        cur["id"] = None
        n_done += 1
        if max_jobs and n_done >= max_jobs:
            break
    STOP.set()
    conn.execute("UPDATE processes SET status='stopped', heartbeat=? WHERE name=?", (time.time(), name))
    LG.info("worker %s exiting after %d jobs", name, n_done)
    return n_done
