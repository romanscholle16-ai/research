"""Supervisor, child-process management, health, and detached launch. Works on Windows and POSIX."""
import json, os, signal, subprocess, sys, time

from . import config, db, exchange, jobs, log, sysinfo

LG = log.get("supervisor")
IS_WIN = sys.platform.startswith("win")
import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]


EPOCH = {"v": ""}


def child_should_exit(conn) -> bool:
    """Orphan protection: a child exits if a newer supervisor epoch exists, or the supervisor has been gone > 10 minutes."""
    mine = os.environ.get("NEXUS_EPOCH", "")
    if not mine:
        return False
    cur = db.get_state(conn, "supervisor_epoch", "")
    if cur and cur != mine:
        return True
    hb = float(db.get_state(conn, "supervisor_heartbeat", "0") or 0)
    return hb > 0 and time.time() - hb > 600


def _env():
    e = dict(os.environ)
    e["NEXUS_EPOCH"] = EPOCH["v"]
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    e["NEXUS_HOME"] = str(config.home())          # children always use the same data directory as the supervisor
    return e


# ----------------------------------------------------------------------------------- locking ----
class Lock:
    def __init__(self, path):
        self.path, self.f = path, None

    def acquire(self) -> bool:
        self.f = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close(); self.f = None
            return False
        self.f.seek(0); self.f.truncate(); self.f.write(str(os.getpid())); self.f.flush()
        return True

    def release(self):
        if self.f:
            try:
                if IS_WIN:
                    import msvcrt
                    self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                self.f.close()
            except OSError:
                pass


def lock_path():
    return config.path("run", "supervisor.lock")


def supervisor_lock_held() -> bool:
    """True if some live process currently owns the supervisor lock (ownership test, not just a PID file)."""
    l = Lock(lock_path())
    got = l.acquire()
    if got:
        l.release()
        return False
    return True


# ------------------------------------------------------------------------------- launching ------
def _popen_detached(args, out_path):
    out = open(out_path, "ab")
    kw = dict(stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(ROOT), env=_env())
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (visible in Task Manager)
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw)


def start_detached(wait=20.0) -> dict:
    """Launch the supervisor independent of the caller (survives closing the terminal / Claude Code)."""
    if supervisor_lock_held():
        return {"started": False, "reason": "supervisor already running (lock held)"}
    env_py = sys.executable
    p = _popen_detached([env_py, "-m", "nexus", "supervisor"], config.path("logs", "supervisor.out"))
    t0 = time.time()
    conn = db.open_db()
    while time.time() - t0 < wait:           # poll the heartbeat, NOT the lock (probing the lock can make the supervisor lose it)
        hb = float(db.get_state(conn, "supervisor_heartbeat", "0") or 0)
        if time.time() - hb < 10 and db.get_state(conn, "supervisor_pid") == str(p.pid):
            return {"started": True, "pid": p.pid}
        if p.poll() is not None:
            return {"started": False, "reason": f"supervisor exited immediately (code {p.returncode}); see logs/supervisor.out", "pid": p.pid}
        time.sleep(0.3)
    return {"started": False, "reason": "supervisor did not report a heartbeat in time", "pid": p.pid}


def send_command(conn, command, args=None):
    with db.tx(conn):
        cur = conn.execute("INSERT INTO commands(ts,command,args,status) VALUES(?,?,?,'new')", (time.time(), command, json.dumps(args or {})))
    return cur.lastrowid


# -------------------------------------------------------------------------------- supervisor ----
class Child:
    def __init__(self, name, role, argv):
        self.name, self.role, self.argv = name, role, argv
        self.proc = None
        self.restarts = []
        self.blocked_until = 0.0
        self.last_exit = None


class Supervisor:
    def __init__(self):
        self.conn = None
        self.children = {}
        self.stop = False

    # -- children -------------------------------------------------------------------------------
    def _spawn(self, ch: Child):
        kw = dict(stdout=open(config.path("logs", f"{ch.name}.out"), "ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(ROOT), env=_env())
        if IS_WIN:
            kw["creationflags"] = 0x00000200
        ch.proc = subprocess.Popen([sys.executable, "-m", "nexus", "child", ch.role, ch.name], **kw)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO processes(name,role,pid,started_at,heartbeat,status,restarts) VALUES(?,?,?,?,?,'starting',?) "
                              "ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, started_at=excluded.started_at, heartbeat=excluded.heartbeat, "
                              "status='starting', restarts=?", (ch.name, ch.role, ch.proc.pid, time.time(), time.time(), len(ch.restarts), len(ch.restarts)))
        LG.info("spawned %s pid=%s", ch.name, ch.proc.pid)

    def _signal(self, ch: Child):
        if ch.proc is None or ch.proc.poll() is not None:
            return
        try:
            if IS_WIN:
                ch.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                ch.proc.terminate()
        except Exception:
            pass

    def _terminate_all(self, chs, timeout=45.0):
        """Signal every child first, then wait for all in parallel; kill only those that ignore the graceful stop."""
        for ch in chs:
            self._signal(ch)
        deadline = time.time() + timeout
        for ch in chs:
            if ch.proc is None:
                continue
            try:
                ch.proc.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                LG.warning("%s ignored graceful stop; killing", ch.name)
                ch.proc.kill()
                ch.proc.wait(10)

    def _terminate(self, ch: Child, timeout=45.0):
        self._terminate_all([ch], timeout)

    def wanted(self, desired, settings):
        w = {"web": ("web", ["web"]), "guardian1": ("guardian", ["1"]), "guardian2": ("guardian", ["2"]), "guardian3": ("guardian", ["3"])}
        out = {"web": "web", "guardian1": "guardian", "guardian2": "guardian", "guardian3": "guardian"}
        if desired != "stopped":
            out["scheduler"] = "scheduler"
            for i in range(1, settings["workers"] + 1):
                out[f"worker-{i}"] = "worker"
        return out

    def ensure_children(self, desired, settings):
        want = self.wanted(desired, settings)
        for name, role in want.items():
            ch = self.children.get(name)
            if ch is None:
                ch = self.children[name] = Child(name, role, None)
            if ch.proc is None or ch.proc.poll() is not None:
                if ch.proc is not None and ch.proc.poll() is not None:
                    self._on_exit(ch)
                    ch.proc = None
                if time.time() >= ch.blocked_until:
                    self._spawn(ch)
        gone = [self.children.pop(n) for n in list(self.children) if n not in want]
        if gone:
            self._terminate_all(gone)
            with db.tx(self.conn):
                for ch in gone:
                    self.conn.execute("UPDATE processes SET status='stopped' WHERE name=?", (ch.name,))

    def _on_exit(self, ch: Child):
        code = ch.proc.returncode
        now = time.time()
        ch.restarts = [t for t in ch.restarts if now - t < 600] + [now]
        ch.last_exit = f"exit code {code} at {time.strftime('%H:%M:%S')}"
        with db.tx(self.conn):
            self.conn.execute("UPDATE processes SET status='exited', last_exit=? WHERE name=?", (ch.last_exit, ch.name))
            if code == 0:
                db.event(self.conn, "process", "supervisor", f"{ch.name} exited cleanly; restarting")
            else:
                db.incident(self.conn, ch.role, "error", f"{ch.name} exited unexpectedly ({ch.last_exit})", "crash or kill",
                            "supervisor restart with exponential backoff", state={"restarts_10min": len(ch.restarts)},
                            fingerprint=f"exit:{ch.name}")
            # requeue anything this process held (never lose or double-run completed work: completion is idempotent)
            for j in self.conn.execute("SELECT id FROM jobs WHERE status='running' AND lease_owner=?", (ch.name,)).fetchall():
                jobs.fail(self.conn, j["id"], f"worker {ch.name} exited ({ch.last_exit})")
        if code != 0:
            n = len(ch.restarts)
            delay = min(300, 2 ** min(n, 8))
            if n > 5:
                delay = 300
                with db.tx(self.conn):
                    db.incident(self.conn, ch.role, "critical", f"{ch.name} is crash-looping ({n} exits/10min)", "persistent fault",
                                "restart throttled to every 5 minutes; failing tasks quarantined by attempt limit", fingerprint=f"crashloop:{ch.name}")
            ch.blocked_until = now + delay

    # -- recovery -------------------------------------------------------------------------------
    def recover(self):
        c = self.conn
        n = 0
        with db.tx(c):
            for j in c.execute("SELECT id,type FROM jobs WHERE status='running'").fetchall():
                jobs.fail(c, j["id"], "reclaimed at supervisor start (previous runtime stopped)")
                n += 1
            c.execute("UPDATE processes SET status='stopped'")
            db.event(c, "recovery", "supervisor", f"startup recovery: {n} interrupted jobs requeued")
        ok, msg = db.integrity_check(c, full=False)
        if not ok:
            with db.tx(c):
                db.incident(c, "database", "critical", f"integrity failure at startup: {msg[:3]}", "corruption", "campaign paused; restore from checkpoint",
                            fingerprint="db_integrity")
            db.set_state(c, "desired", "paused")
        elif not c.execute("SELECT 1 FROM checkpoints WHERE ts>?", (time.time() - 6 * 3600,)).fetchone():
            db.backup(c, kind="periodic")
        db.set_state(c, "last_recovery", json.dumps({"ts": time.time(), "requeued": n, "db_ok": ok}))
        return n

    # -- commands -------------------------------------------------------------------------------
    def handle_commands(self):
        c = self.conn
        for cmd in c.execute("SELECT * FROM commands WHERE status='new' ORDER BY id").fetchall():
            args = json.loads(cmd["args"] or "{}")
            res = "ok"
            k = cmd["command"]
            with db.tx(c):
                if k in ("start", "resume"):
                    if db.get_state(c, "desired") != "running":
                        db.set_state(c, "running_since", time.time())
                    db.set_state(c, "desired", "running")
                    db.set_state(c, "shutdown_requested", "0")
                elif k == "pause":
                    db.set_state(c, "desired", "paused")
                elif k == "stop":
                    db.set_state(c, "desired", "stopped")
                elif k == "shutdown":
                    db.set_state(c, "desired", "stopped")
                    db.set_state(c, "shutdown_requested", "1")
                    self.stop = True
                elif k == "restart_worker":
                    db.set_state(c, f"restart:{args.get('name')}", "1")
                else:
                    res = "unknown command"
                c.execute("UPDATE commands SET status='done', result=? WHERE id=?", (res, cmd["id"]))
                db.event(c, "command", "supervisor", f"command {k} {args}")

    def restart_flags(self):
        for r in self.conn.execute("SELECT key FROM runtime_state WHERE key LIKE 'restart:%' AND value='1'").fetchall():
            name = r[0].split(":", 1)[1]
            ch = self.children.get(name)
            if ch:
                LG.info("restart requested for %s", name)
                self._terminate(ch)
                with db.tx(self.conn):
                    for j in self.conn.execute("SELECT id FROM jobs WHERE status='running' AND lease_owner=?", (name,)).fetchall():
                        jobs.fail(self.conn, j["id"], "worker restarted on request")
                    db.event(self.conn, "process", "supervisor", f"{name} restarted on request")
                ch.proc = None
                ch.blocked_until = 0
            db.set_state(self.conn, r[0], "0")

    # -- main loop ------------------------------------------------------------------------------
    def run(self):
        lock = Lock(lock_path())
        for _ in range(25):                      # tolerate a transient probe by `nexus status/start`
            if lock.acquire():
                break
            time.sleep(0.2)
        else:
            print("supervisor already running (lock held); exiting", file=sys.stderr)
            return 3
        self.conn = db.open_db()
        with db.tx(self.conn):
            exchange.seed(self.conn)
            import uuid
            EPOCH["v"] = uuid.uuid4().hex
            db.set_state(self.conn, "supervisor_epoch", EPOCH["v"])        # supersedes any orphaned children of a dead supervisor
            db.set_state(self.conn, "supervisor_pid", os.getpid())
            db.set_state(self.conn, "supervisor_started", time.time())
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: setattr(self, "stop", True))
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, lambda *_: setattr(self, "stop", True))
        n = self.recover()
        LG.info("supervisor started pid=%s; recovered %d jobs; desired=%s", os.getpid(), n, db.get_state(self.conn, "desired"))
        prev_desired = db.get_state(self.conn, "desired")
        if prev_desired == "running":
            db.set_state(self.conn, "running_since", time.time())
        try:
            while not self.stop:
                db.set_state(self.conn, "supervisor_heartbeat", time.time())
                self.handle_commands()
                if self.stop:
                    break
                desired = db.get_state(self.conn, "desired", "stopped")
                self.ensure_children(desired, db.settings(self.conn))
                self.restart_flags()
                time.sleep(1.0)
        finally:
            LG.info("supervisor shutting down gracefully")
            db.set_state(self.conn, "desired", "stopped")
            self._terminate_all(list(self.children.values()))
            with db.tx(self.conn):
                self.conn.execute("UPDATE processes SET status='stopped'")
                db.event(self.conn, "supervisor", "supervisor", "graceful shutdown complete")
                db.set_state(self.conn, "supervisor_heartbeat", 0)
            lock.release()
        return 0


# ---------------------------------------------------------------------------------- children ----
def child_main(role, name):
    conn = db.open_db()
    pid = os.getpid()
    with db.tx(conn):
        conn.execute("INSERT INTO processes(name,role,pid,started_at,heartbeat,status) VALUES(?,?,?,?,?,'running') ON CONFLICT(name) DO UPDATE SET "
                     "pid=excluded.pid, started_at=excluded.started_at, heartbeat=excluded.heartbeat, status='running'",
                     (name, role, pid, time.time(), time.time()))
    stop = {"v": False}
    def _s(*_): stop["v"] = True
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _s)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _s)
    lg = log.get(name)
    lg.info("%s started pid=%s", name, pid)
    if role == "worker":
        from . import worker
        worker.run(name)
    elif role == "scheduler":
        from . import scheduler
        import random
        rng = random.Random()
        last_periodic = 0
        while not stop["v"]:
            try:
                conn.execute("UPDATE processes SET heartbeat=? WHERE name=? AND pid=?", (time.time(), name, pid))
                if child_should_exit(conn):
                    break
                if time.time() - last_periodic > 60:
                    scheduler.periodic(conn)
                    last_periodic = time.time()
                scheduler.tick(conn, rng)
            except Exception as e:
                lg.exception("scheduler error")
                with db.tx(conn):
                    db.incident(conn, "scheduler", "error", f"{type(e).__name__}: {e}", "scheduler exception", "retry next tick", fingerprint="sched_err")
            time.sleep(2.0)
    elif role == "guardian":
        from . import guardians
        g = guardians.GUARDIANS[int(name[-1])](db.open_db())
        while not stop["v"]:
            try:
                g.conn.execute("UPDATE processes SET heartbeat=? WHERE name=? AND pid=?", (time.time(), name, pid))
                if child_should_exit(g.conn):
                    break
                g.cycle()
            except Exception as e:
                lg.exception("guardian error")
                try:
                    g.raise_incident("guardian", "error", f"{name}: {type(e).__name__}: {e}", "guardian exception", "continue next cycle", fp=f"gerr:{name}")
                except Exception:
                    pass
            time.sleep(g.interval)
    elif role == "web":
        from . import server
        server.serve(stop)
    with db.tx(conn):
        conn.execute("UPDATE processes SET status='stopped', heartbeat=? WHERE name=? AND pid=?", (time.time(), name, pid))
    lg.info("%s exiting", name)


# ----------------------------------------------------------------------------------- health ----
def health(conn=None):
    """Truthful runtime status. Never green unless the supervisor is alive AND (if running) work is progressing."""
    conn = conn or db.open_db()
    now = time.time()
    sh = float(db.get_state(conn, "supervisor_heartbeat", "0") or 0)
    sup_age = now - sh
    lock = supervisor_lock_held()
    desired = db.get_state(conn, "desired", "stopped")
    sup_alive = lock and sup_age < 30
    procs = [dict(r) for r in conn.execute("SELECT * FROM processes ORDER BY role, name")]
    for p in procs:
        p["alive"] = sysinfo.pid_alive(p["pid"]) and p["status"] in ("running", "starting")
        p["hb_age"] = round(now - (p["heartbeat"] or 0), 1)
    workers = [p for p in procs if p["role"] == "worker" and p["alive"] and p["hb_age"] < 60]
    q = jobs.counts(conn)
    done_5m = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done' AND finished_at>?", (now - 300,)).fetchone()[0]
    done_1m = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done' AND finished_at>?", (now - 60,)).fetchone()[0]
    open_crit = conn.execute("SELECT COUNT(*) FROM incidents WHERE resolved_at IS NULL AND severity='critical'").fetchone()[0]
    reasons = []
    if not sup_alive:
        status = "offline" if db.get_state(conn, "shutdown_requested") == "1" and not lock else "dead"
        reasons.append("supervisor is not running" if not lock else f"supervisor heartbeat stale ({sup_age:.0f}s)")
    elif desired == "stopped":
        status = "stopped"
    elif desired == "paused":
        status = "paused"
    else:
        status = "healthy"
        if not workers:
            status = "degraded"; reasons.append("no live workers")
        elif q["pending"] > 0 and done_5m == 0:
            status = "stalled"; reasons.append(f"{q['pending']} jobs queued but none completed in 5 minutes")
        elif q["pending"] == 0 and q["running"] == 0 and done_5m == 0:
            status = "idle"; reasons.append("no work queued (scheduler waiting or space exhausted)")
        if status == "healthy" and open_crit:
            status = "degraded"; reasons.append(f"{open_crit} critical incident(s) open")
        if status == "healthy" and db.get_state(conn, "throttle") == "high":
            status = "degraded"; reasons.append("throttled by memory pressure")
    return {"status": status, "reasons": reasons, "desired": desired, "supervisor_alive": sup_alive, "supervisor_pid": db.get_state(conn, "supervisor_pid"),
            "supervisor_heartbeat_age_s": round(sup_age, 1) if sh else None, "workers_alive": len(workers), "queue": q,
            "jobs_done_last_5m": done_5m, "jobs_done_last_1m": done_1m, "processes": procs, "open_critical": open_crit,
            "throttle": db.get_state(conn, "throttle", "none"), "live_trading_enabled": False, "time": now}
