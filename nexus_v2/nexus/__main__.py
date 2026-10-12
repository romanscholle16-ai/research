import argparse, json, os, subprocess, sys, time


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nexus", description="NEXUS V2 research runtime (research only; live trading is impossible)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("start", "pause", "resume", "stop", "shutdown", "status", "init", "sync-data", "backup", "integrity", "test", "install-startup",
              "uninstall-startup", "supervisor", "state-report", "rebaseline"):
        sp.add_parser(c)
    ch = sp.add_parser("child"); ch.add_argument("role"); ch.add_argument("name")
    rs = sp.add_parser("restore"); rs.add_argument("backup")
    rw = sp.add_parser("restart-worker"); rw.add_argument("name")
    ac = sp.add_parser("approve-code-change"); ac.add_argument("reason")
    a = ap.parse_args(argv)

    from . import config, db, runtime
    if a.cmd == "supervisor":
        return runtime.Supervisor().run()
    if a.cmd == "child":
        return runtime.child_main(a.role, a.name)
    conn = db.open_db()
    if a.cmd == "init":
        from . import exchange
        with db.tx(conn):
            exchange.seed(conn)
        print("initialised", config.db_path()); return 0
    if a.cmd == "sync-data":
        from . import data
        for s in config.DATA["symbols"]:
            print(s, data.sync_symbol(conn, s)); print(data.build_dataset(conn, s)["version"])
        return 0
    if a.cmd == "start":
        pending_sd = conn.execute("SELECT 1 FROM commands WHERE command='shutdown' AND status='new'").fetchone()
        if runtime.supervisor_lock_held() and (pending_sd or db.get_state(conn, "shutdown_requested") == "1"):
            print("previous supervisor is still shutting down; waiting for it to exit...")
            for _ in range(180):
                if not runtime.supervisor_lock_held():
                    break
                time.sleep(0.5)
        r = runtime.start_detached()
        print("supervisor:", r)
        if r.get("started") or "already" in r.get("reason", ""):
            runtime.send_command(conn, "start")
            for _ in range(40):
                time.sleep(0.5)
                if conn.execute("SELECT status FROM commands ORDER BY id DESC LIMIT 1").fetchone()[0] == "done":
                    break
            print("research campaign start requested; desired state =", db.get_state(conn, "desired"))
        return 0
    if a.cmd in ("pause", "resume", "stop", "shutdown"):
        if not runtime.supervisor_lock_held():
            print("no supervisor is running"); return 1
        runtime.send_command(conn, a.cmd); print(f"{a.cmd} requested")
        if a.cmd == "shutdown":                       # block until the supervisor has really exited
            for _ in range(240):
                if not runtime.supervisor_lock_held():
                    print("supervisor exited cleanly"); return 0
                time.sleep(0.5)
            print("supervisor still running after 120s"); return 1
        return 0
    if a.cmd == "restart-worker":
        runtime.send_command(conn, "restart_worker", {"name": a.name}); print("requested"); return 0
    if a.cmd == "status":
        h = runtime.health(conn)
        from . import memory
        print(json.dumps({k: h[k] for k in ("status", "reasons", "desired", "supervisor_alive", "supervisor_pid", "workers_alive", "queue",
                                            "jobs_done_last_5m", "open_critical", "throttle", "live_trading_enabled")}, indent=1))
        print(json.dumps(memory.counters(conn), indent=1))
        return 0 if h["status"] in ("healthy", "idle", "paused", "stopped") else 2
    if a.cmd == "backup":
        print(db.backup(conn, kind="manual")); return 0
    if a.cmd == "integrity":
        ok, msg = db.integrity_check(conn, True); print(ok, msg[:5]); return 0 if ok else 1
    if a.cmd == "restore":
        if runtime.supervisor_lock_held():
            print("stop the runtime first (nexus shutdown)"); return 1
        db.restore(a.backup); print("restored (previous DB kept as .pre-restore-*)"); return 0
    if a.cmd == "approve-code-change":
        from . import guardians
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", os.path.join(os.path.dirname(__file__), "..", "tests")], capture_output=True, text=True)
        failed = 0 if r.returncode == 0 else 1
        guardians.approve_code_baseline(conn, a.reason, {"failed": failed, "output": r.stdout[-500:]})
        print("baseline updated; tests:", r.stdout.strip().splitlines()[-1]); return 0
    if a.cmd == "test":
        import re
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", os.path.join(os.path.dirname(__file__), "..", "tests")], capture_output=True, text=True)
        print(r.stdout[-3000:])
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        n = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|skipped|error)", last)}
        with db.tx(conn):
            conn.execute("INSERT INTO test_evidence(ts,suite,passed,failed,skipped,output) VALUES(?,?,?,?,?,?)",
                         (time.time(), "full", n.get("passed", 0), n.get("failed", 0) + n.get("error", 0), n.get("skipped", 0), r.stdout[-2000:]))
        return r.returncode
    if a.cmd in ("install-startup", "uninstall-startup"):
        from . import startup
        return startup.run(a.cmd == "install-startup")
    if a.cmd == "rebaseline":
        db.rebaseline(conn); print("baseline re-recorded"); return 0
    if a.cmd == "state-report":
        from . import statefile
        print(statefile.write(conn)); return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
