"""One-command launch with REAL verification (not PID existence). Used by START_NEXUS.bat / CHECK_NEXUS.bat.
Exit codes: 0 all checks passed | 1 a check failed | 2 launch refused by preflight."""
import json, os, socket, sys, time, urllib.request, webbrowser

from . import __version__, config, db, exchange, jobs, memory, runtime, sysinfo, log

LG = log.get("launcher")
PORT = lambda: int(os.environ.get("NEXUS_PORT", "8765"))


def _row(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": str(detail)}


def apply_profile(conn, name):
    if name not in config.PROFILES:
        raise ValueError(f"unknown profile {name!r}; available: {sorted(config.PROFILES)}")
    upd = config.validate_settings(config.PROFILES[name])
    cur = db.settings(conn)
    cur.update(upd)
    with db.tx(conn):
        db.set_state(conn, "settings", json.dumps(cur))
        db.set_state(conn, "profile", name)
        db.event(conn, "profile", "launcher", f"profile {name} applied: {upd}")
    return cur


def port_owner_ok(port):
    """True if the port is free OR answered by our own dashboard; False if some other program owns it."""
    s = socket.socket()
    s.settimeout(1.0)
    try:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return True, "free"
    finally:
        s.close()
    try:
        d = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).read())
        return ("status" in d and d.get("live_trading_enabled") is False), "owned by NEXUS dashboard"
    except Exception:
        return False, "port is in use by another program"


def preflight(conn=None):
    """Checks that must hold BEFORE anything is started. Returns list of rows."""
    r = []
    r.append(_row("platform", True, f"{sys.platform} python {sys.version.split()[0]} nexus {__version__}"))
    r.append(_row("python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    try:
        import numpy
        r.append(_row("numpy import", True, numpy.__version__))
    except Exception as e:
        r.append(_row("numpy import", False, e))
    r.append(_row("live trading disabled", config.RISK["live_trading"] is False and not __import__("nexus").LIVE_TRADING_ENABLED))
    try:
        conn = conn or db.open_db()
        ok, msg = db.integrity_check(conn, full=False)
        r.append(_row("database integrity", ok, "; ".join(msg[:2])))
        base = conn.execute("SELECT value FROM meta WHERE key='trusted_hash_baseline'").fetchone()[0]
        same = base == config.trusted_hash()
        r.append(_row("trusted config matches baseline", same,
                      "ok" if same else "config differs from baseline: run `python -m nexus approve-change \"<reason>\"` (reviewed change) or restore config"))
        with db.tx(conn):
            exchange.seed(conn)
    except Exception as e:
        r.append(_row("database open", False, f"{type(e).__name__}: {e}"))
    d = sysinfo.disk(config.home())
    st = db.settings(conn) if conn else config.default_settings()
    r.append(_row("disk space", d["free_gb"] >= st["disk_min_free_gb"], f"{d['free_gb']} GB free (min {st['disk_min_free_gb']})"))
    m = sysinfo.mem()
    r.append(_row("memory headroom", m["used_pct"] < st["mem_pause_pct"], f"{m['used_pct']}% used"))
    ok, why = port_owner_ok(PORT())
    r.append(_row(f"dashboard port {PORT()}", ok, why))
    r.append(_row("gpu (informational)", True, json.dumps(sysinfo.gpu())))
    return r


def verify(conn=None, sample_s=6.0, progress_wait_s=0.0, expect_workers=None):
    """Real health evidence: ownership, heartbeats ADVANCING, DB write/read, persisted progress, dashboard, incidents."""
    conn = conn or db.open_db()
    st = db.settings(conn)
    expect_workers = expect_workers or st["workers"]
    now = time.time()
    r = []
    held = runtime.supervisor_lock_held()
    sh = float(db.get_state(conn, "supervisor_heartbeat", "0") or 0)
    r.append(_row("supervisor owns lock + heartbeat fresh", held and now - sh < 20, f"lock={held} heartbeat_age={now - sh:.1f}s pid={db.get_state(conn, 'supervisor_pid')}"))
    snap1 = {p["name"]: (p["pid"], p["heartbeat"]) for p in conn.execute("SELECT name,pid,heartbeat FROM processes WHERE status IN ('running','starting')")}
    time.sleep(sample_s)
    rows = {p["name"]: p for p in conn.execute("SELECT * FROM processes WHERE status IN ('running','starting')")}
    need = ["web", "scheduler", "guardian1", "guardian2", "guardian3"] + [f"worker-{i}" for i in range(1, expect_workers + 1)]
    for n in need:
        p = rows.get(n)
        if not p:
            r.append(_row(f"process {n}", False, "no live row")); continue
        alive = sysinfo.pid_alive(p["pid"])
        adv = p["heartbeat"] > snap1.get(n, (None, 0))[1] or (time.time() - p["heartbeat"]) < 12
        r.append(_row(f"process {n}", alive and adv and time.time() - p["heartbeat"] < 30,
                      f"pid={p['pid']} alive={alive} heartbeat_age={time.time() - p['heartbeat']:.1f}s"))
    extra = [n for n in rows if n.startswith("worker-") and n not in need]
    r.append(_row("no duplicate/extra workers", not extra, f"extra={extra}"))
    # database write + read-back
    tag = f"probe-{time.time()}"
    try:
        with db.tx(conn):
            db.event(conn, "probe", "launcher", tag)
        ok = conn.execute("SELECT 1 FROM events WHERE message=?", (tag,)).fetchone() is not None
        r.append(_row("database write/read", ok, "event round-trip"))
    except Exception as e:
        r.append(_row("database write/read", False, e))
    ok, msg = db.integrity_check(conn, full=False)
    r.append(_row("database integrity", ok, "; ".join(msg[:2])))
    # persisted progress or a legitimate wait state
    t0 = time.time()
    k0 = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
    while True:
        k1 = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
        boot = conn.execute("SELECT type,heartbeat FROM jobs WHERE status='running' AND type IN ('data_update','validate')").fetchall()
        recent = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='done' AND finished_at>?", (time.time() - 300,)).fetchone()[0]
        if k1 > k0 or boot or recent or time.time() - t0 >= progress_wait_s:
            break
        time.sleep(2)
    has_data = conn.execute("SELECT 1 FROM datasets LIMIT 1").fetchone() is not None
    if k1 > k0 or recent:
        r.append(_row("research progress (persisted)", True, f"jobs done +{k1 - k0} during check, {recent} in last 5 min"))
    elif boot and any(time.time() - b["heartbeat"] < 60 for b in boot):
        r.append(_row("research progress (legitimate wait)", True, f"long job running with live heartbeat: {[b['type'] for b in boot]}" + ("" if has_data else " (first-time data download)")))
    else:
        pend = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        r.append(_row("research progress", False, f"no job completed recently; pending={pend}; datasets={'yes' if has_data else 'NO'}"))
    # dashboard
    try:
        h = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT()}/api/health", timeout=5).read())
        r.append(_row("dashboard responds", True, f"http://127.0.0.1:{PORT()} status={h['status']}"))
        r.append(_row("health endpoint truthful (not dead)", h["status"] not in ("dead", "stalled"), h["status"] + " " + "; ".join(h["reasons"])))
        r.append(_row("live trading disabled (runtime)", h.get("live_trading_enabled") is False))
    except Exception as e:
        r.append(_row("dashboard responds", False, f"{type(e).__name__}: {e}"))
    crit = conn.execute("SELECT COUNT(*),GROUP_CONCAT(error,' | ') FROM incidents WHERE resolved_at IS NULL AND severity='critical'").fetchone()
    r.append(_row("no unresolved critical incidents", crit[0] == 0, (crit[1] or "none")[:200]))
    d = sysinfo.disk(config.home()); m = sysinfo.mem()
    r.append(_row("disk/memory within limits", d["free_gb"] >= st["disk_min_free_gb"] and m["used_pct"] < st["mem_pause_pct"], f"disk free {d['free_gb']} GB, RAM {m['used_pct']}%"))
    return r


def report(rows, title):
    bad = [x for x in rows if not x["ok"]]
    print(f"\n=== {title} ===")
    for x in rows:
        print(f"  [{'PASS' if x['ok'] else 'FAIL'}] {x['check']}: {x['detail']}")
    print(f"=== {'ALL CHECKS PASSED' if not bad else str(len(bad)) + ' CHECK(S) FAILED'} ===")
    LG.info("%s: %s", title, json.dumps(rows))
    return 0 if not bad else 1


def launch(profile="unattended_7_day", open_browser=True):
    conn = db.open_db()
    pre = preflight(conn)
    fatal = [x for x in pre if not x["ok"]]
    if fatal:
        report(pre, "PREFLIGHT")
        print("Launch refused: fix the failed preflight checks above.")
        return 2
    held = runtime.supervisor_lock_held()
    apply_profile(conn, profile)
    if held:
        print("A supervisor already owns this installation: not starting a second one (duplicate prevention).")
    else:
        res = runtime.start_detached()
        print("supervisor:", res)
        if not res.get("started"):
            print("Supervisor failed to start; see var/logs/supervisor.out")
            return 1
    runtime.send_command(conn, "start")
    for _ in range(40):
        time.sleep(0.5)
        if db.get_state(conn, "desired") == "running":
            break
    has_data = conn.execute("SELECT 1 FROM datasets LIMIT 1").fetchone() is not None
    wait = 90 if has_data else 900          # first run: the data download is the legitimate wait state
    print(f"verifying (up to {wait}s for first persisted progress)...")
    t0 = time.time()
    rows = []
    while time.time() - t0 < wait + 60:
        time.sleep(5)
        rows = verify(conn, sample_s=6.0, progress_wait_s=0)
        if all(x["ok"] for x in rows):
            break
    rc = report(rows, f"LAUNCH VERIFICATION (profile {profile})")
    if rc == 0 and open_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT()}/")
        except Exception:
            pass
    return rc


def check():
    return report(verify(db.open_db(), sample_s=6.0, progress_wait_s=20), "NEXUS HEALTH CHECK")
