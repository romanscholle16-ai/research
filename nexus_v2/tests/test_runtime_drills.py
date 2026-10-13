"""Gates G/H: recovery drills and an end-to-end bounded campaign using REAL supervisor/worker/guardian processes in an isolated
NEXUS_HOME with synthetic (test-only) data. The production database is never touched."""
import json, os, signal, subprocess, sys, time, pathlib, sqlite3
import pytest
from nexus import db, jobs, runtime, sysinfo, ai, guardians, worker, config, memory
import conftest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def hard_kill(pid):
    """Equivalent of kill -9 on POSIX and `taskkill /F` on Windows."""
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


class RT:
    def __init__(self, home):
        self.home = str(home)
        self.env = {**os.environ, "NEXUS_HOME": self.home, "NEXUS_NO_RELAUNCH": "", "NEXUS_PORT": str(19000 + os.getpid() % 800), "PYTHONPATH": str(ROOT)}

    def cli(self, *a, timeout=150):
        r = subprocess.run([sys.executable, "-m", "nexus", *a], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr

    def conn(self):
        return db.connect(pathlib.Path(self.home) / "nexus.db")

    def wait(self, cond, timeout=60, step=0.5):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                v = cond()
                if v:
                    return v
            except sqlite3.Error:
                pass
            time.sleep(step)
        raise AssertionError("timeout waiting for condition")

    def health(self):
        return json.loads(self.cli("status").split("\n}")[0] + "\n}")


@pytest.fixture()
def rt(tmp_path):
    home = tmp_path / "nx"
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(30000, 7)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(30000, 8))
    r = RT(home)
    os.environ["NEXUS_HOME"] = str(home)
    c = db.open_db(home / "nexus.db")
    c.execute("INSERT INTO datasets(symbol,kind,timeframe,rows,start_ts,end_ts,gaps,dupes,bad_rows,version,data_class,manifest_json) VALUES('ETHUSDT','klines','1h',30000,0,1,0,0,0,'v','verified','{}')")
    with db.tx(c):
        db.set_state(c, "settings", json.dumps({**config.default_settings(), "agent_budget_per_hour": 600, "workers": 2}))
        # keep data_update from hitting the network during tests: mark the periodic jobs as already done for this time bucket
        now = time.time()
        for k in (f"data:{int(now // (6 * 3600))}", f"pf:{int(now // 86400)}"):
            c.execute("INSERT INTO jobs(type,payload,status,priority,created_at,dedupe_key,finished_at) VALUES('noop','{}','done',9,?,?,?)", (now, k, now))
    c.close()
    yield r
    r.cli("shutdown", timeout=200)


def _children(rt):
    c = rt.conn()
    return {r["name"]: r for r in c.execute("SELECT * FROM processes WHERE status IN ('running','starting')")}


def test_end_to_end_campaign_and_independence_from_caller(rt):
    """Gate H: start -> experiments persist -> candidates validated -> dashboard shows truth. The launching process
    exits immediately (like closing Claude Code) and the runtime keeps going."""
    out = rt.cli("start")
    assert "'started': True" in out and "desired state = running" in out
    c = rt.conn()
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 60, 90)
    h = rt.health()
    assert h["status"] in ("healthy", "idle") and h["workers_alive"] == 2 and h["live_trading_enabled"] is False
    assert c.execute("SELECT COUNT(*) FROM scheduler_log WHERE why!=''").fetchone()[0] > 0
    assert c.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0] > 0
    # processes exist as independent OS processes owned by init/ the supervisor, not by this test's shell
    sup = int(db.get_state(c, "supervisor_pid"))
    assert sysinfo.pid_alive(sup)
    ch = _children(rt)
    assert {"web", "scheduler", "worker-1", "worker-2", "guardian1", "guardian2", "guardian3"} <= set(ch)
    # regression: worker heartbeat thread must keep ticking (a cross-thread sqlite bug once made it silently fail)
    t_a = {p["name"]: p["heartbeat"] for p in _children(rt).values() if p["name"].startswith("worker")}
    time.sleep(11)
    t_b = {p["name"]: p["heartbeat"] for p in _children(rt).values() if p["name"].startswith("worker")}
    assert t_b and all(t_b[n] > t_a[n] + 4 for n in t_a), (t_a, t_b)
    assert c.execute("SELECT COUNT(*) FROM incidents WHERE error LIKE '%unresponsive%'").fetchone()[0] == 0
    n1 = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] > n1, 90)      # still progressing with no caller attached (pacing: <=10/agent/min)
    # durable counts == raw rows (no double counting)
    assert c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == c.execute("SELECT COUNT(DISTINCT strategy_id) FROM experiments").fetchone()[0]
    import urllib.request
    ov = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{rt.env['NEXUS_PORT']}/api/overview", timeout=20).read())
    assert ov["health"]["supervisor_alive"] and len(ov["goals"]) == 15 and ov["live_trading"] is False


def test_worker_killed_mid_task_is_restarted_without_double_counting(rt):
    rt.cli("start")
    c = rt.conn()
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 20, 90)
    ch = _children(rt)
    victim = ch["worker-1"]
    hard_kill(victim["pid"])
    new = rt.wait(lambda: (_children(rt).get("worker-1") or {"pid": victim["pid"]})["pid"] != victim["pid"] and _children(rt).get("worker-1"), 60)
    assert new["pid"] != victim["pid"]
    assert c.execute("SELECT COUNT(*) FROM incidents WHERE fingerprint='exit:worker-1'").fetchone()[0] >= 1       # cause recorded
    n = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] > n + 10, 120)
    assert c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == c.execute("SELECT COUNT(DISTINCT strategy_id||dataset_version) FROM experiments").fetchone()[0]
    assert not c.execute("SELECT 1 FROM jobs WHERE status='running' AND lease_pid=?", (victim["pid"],)).fetchone()   # nothing stuck on the dead pid
    assert db.integrity_check(c)[0]


def test_duplicate_supervisor_prevented(rt):
    rt.cli("start")
    out = rt.cli("start")
    assert "already running" in out
    r = subprocess.run([sys.executable, "-m", "nexus", "supervisor"], cwd=ROOT, env=rt.env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3 and "already running" in r.stderr
    c = rt.conn()
    workers = [n for n in _children(rt) if n.startswith("worker")]
    assert sorted(workers) == ["worker-1", "worker-2"]                                            # no duplicate workers


def test_supervisor_killed_reports_dead_then_recovers_cleanly(rt):
    rt.cli("start")
    c = rt.conn()
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 30, 90)
    old_children = _children(rt)
    sup = int(db.get_state(c, "supervisor_pid"))
    done_before = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    hard_kill(sup)
    time.sleep(1.5)
    h = json.loads(rt.cli("status").split("\n}")[0] + "\n}")
    assert h["status"] == "dead" and h["supervisor_alive"] is False                              # never shown as healthy
    out = rt.cli("start")                                                                          # what a Windows scheduled task / guardian relaunch does
    assert "'started': True" in out
    rt.wait(lambda: float(db.get_state(rt.conn(), "supervisor_heartbeat", "0")) > time.time() - 5, 30)
    rec = json.loads(db.get_state(rt.conn(), "last_recovery"))
    assert rec["db_ok"] is True
    # orphaned children of the dead supervisor are superseded (epoch) -> no duplicate workers
    def only_new():
        for name, p in old_children.items():
            if sysinfo.pid_alive(p["pid"]) and name.startswith("worker"):
                return False
        return True
    rt.wait(only_new, 60)
    rt.wait(lambda: len([n for n in _children(rt) if n.startswith("worker")]) == 2, 60)
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] > done_before + 10, 150)  # completed work kept, new work continues
    assert db.integrity_check(c)[0]


def test_stop_is_graceful_and_resume_continues(rt):
    rt.cli("start")
    c = rt.conn()
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 30, 90)
    rt.cli("stop")
    rt.wait(lambda: db.get_state(rt.conn(), "desired") == "stopped" and not [n for n in _children(rt) if n.startswith(("worker", "scheduler"))], 90)
    n = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    assert n >= 30 and db.integrity_check(c)[0]
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0] == 0           # nothing abandoned mid-flight
    h = rt.health()
    assert h["status"] == "stopped" and h["supervisor_alive"]                                         # dashboard/guardians still up
    rt.cli("start")
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] > n + 10, 90)


def test_repeated_failure_quarantines_without_blocking_other_work(rt):
    c = rt.conn()
    with db.tx(c):
        jobs.enqueue(c, "experiment", {"spec": {"family": "donchian", "symbol": "ETHUSDT", "timeframe": "1h", "params": {"n": 21}}, "agent": "x"},
                     dedupe="poison", max_attempts=2)       # off-grid spec raises every time
    c.execute("UPDATE jobs SET not_before=0")
    rt.cli("start")
    def quarantined():
        jobs_ = rt.conn()
        jobs_.execute("UPDATE jobs SET not_before=0 WHERE dedupe_key='poison' AND status='pending'")
        return jobs_.execute("SELECT status FROM jobs WHERE dedupe_key='poison'").fetchone()[0] == "quarantined"
    rt.wait(quarantined, 90)
    assert c.execute("SELECT COUNT(*) FROM incidents WHERE component='worker'").fetchone()[0] >= 1
    rt.wait(lambda: c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] >= 15, 90)             # healthy work unaffected
    assert db.integrity_check(c)[0]


def test_stalled_job_with_dead_worker_reclaimed_by_guardian(conn):
    with db.tx(conn):
        jid = jobs.enqueue(conn, "validate", {"strategy_id": "x"}, dedupe="stall")
    jobs.claim(conn, "ghost", 4_000_000)
    conn.execute("UPDATE jobs SET heartbeat=?", (time.time() - 1000,))
    db.set_state(conn, "supervisor_heartbeat", time.time()); db.set_state(conn, "desired", "running")
    guardians.Reliability(conn).cycle()
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] in ("pending", "quarantined")
    assert conn.execute("SELECT COUNT(*) FROM incidents WHERE component='queue'").fetchone()[0] == 1


def test_resource_breach_throttles_then_releases(conn, monkeypatch):
    db.set_state(conn, "desired", "running"); db.set_state(conn, "supervisor_heartbeat", time.time())
    g = guardians.Reliability(conn)
    monkeypatch.setattr(sysinfo, "mem", lambda: {"total_mb": 16000, "avail_mb": 100, "used_pct": 99.0})
    g.cycle()
    assert db.get_state(conn, "throttle") == "high"
    with db.tx(conn):
        jobs.enqueue(conn, "experiment", {}, dedupe="t1")
    assert worker.run("w-test", once=True, max_jobs=1) == 0                                          # worker refuses to take work under pressure
    assert conn.execute("SELECT status FROM jobs WHERE dedupe_key='t1'").fetchone()[0] == "pending"
    monkeypatch.setattr(sysinfo, "mem", lambda: {"total_mb": 16000, "avail_mb": 9000, "used_pct": 40.0})
    g.last.clear(); g.cycle()
    assert db.get_state(conn, "throttle") == "none"


def test_low_disk_pauses_campaign_and_records_incident(conn, monkeypatch):
    db.set_state(conn, "desired", "running"); db.set_state(conn, "supervisor_heartbeat", time.time())
    monkeypatch.setattr(sysinfo, "disk", lambda p: {"free_gb": 0.2, "total_gb": 100, "used_pct": 99.8})
    guardians.Reliability(conn).cycle()
    assert db.get_state(conn, "desired") == "paused"
    assert conn.execute("SELECT severity FROM incidents WHERE fingerprint='disk_low'").fetchone()[0] == "critical"


def test_database_corruption_detected_pauses_and_backup_restorable(home):
    c = db.open_db()
    with db.tx(c):
        db.event(c, "t", "c", "important")
    good = db.backup(c, kind="manual")
    c.close()
    p = config.db_path()
    raw = bytearray(p.read_bytes())
    for i in range(4096 * 3, 4096 * 3 + 2000):
        raw[i] ^= 0xFF
    p.write_bytes(bytes(raw))
    for ext in ("-wal", "-shm"):
        pathlib.Path(str(p) + ext).unlink(missing_ok=True)
    try:
        c2 = db.connect()
        ok, msg = db.integrity_check(c2, False)
    except sqlite3.DatabaseError:
        ok = False
    assert not ok
    db.restore(good["path"])
    c3 = db.open_db()
    assert db.integrity_check(c3)[0] and c3.execute("SELECT COUNT(*) FROM events WHERE message='important'").fetchone()[0] == 1


def test_ai_provider_unavailable_falls_back_with_backoff():
    calls = {"n": 0}
    class Down(ai.Provider):
        name = "down"
        def generate(self, prompt, timeout=1):
            calls["n"] += 1
            raise ConnectionError("unreachable")
    t = {"v": 1000.0}
    r = ai.Router([Down()], now=lambda: t["v"])
    for _ in range(5):
        out, who = r.generate("summarise")
        assert who == "deterministic" and out
    assert calls["n"] == 1                                    # circuit breaker: not hammered while backing off
    t["v"] += 130
    r.generate("x"); assert calls["n"] == 2                   # retried only after backoff expires
    assert r.status()["down"]["consecutive_failures"] == 2


def test_core_operation_needs_no_ai_credentials_or_paid_api():
    src = "".join(p.read_text() for p in (ROOT / "nexus").glob("*.py") if p.name != "ai.py")
    assert "openai" not in src.lower() and "anthropic" not in src.lower() and "api_key" not in src.lower()


def test_no_live_order_or_withdrawal_code_paths():
    src = "".join(p.read_text().lower() for p in (ROOT / "nexus").glob("*.py"))
    for forbidden in ("/fapi/v1/order", "create_order", "withdraw(", "place_order", "/sapi/v1/capital/withdraw", "private_key"):
        assert forbidden not in src
    import nexus
    assert nexus.LIVE_TRADING_ENABLED is False and config.RISK["live_trading"] is False


def test_launch_command_verifies_real_health_and_survives_launcher_exit(rt):
    """`nexus launch` (what START_NEXUS.bat calls) must return 0 only after ownership, heartbeats, DB round-trip, persisted progress,
    dashboard and incident checks pass; a second launch must refuse to start a duplicate; `check` must agree; stop must be clean."""
    def run(*a, timeout=400):
        r = subprocess.run([sys.executable, "-m", "nexus", *a], cwd=ROOT, env=rt.env, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    rc, out = run("launch", "--no-browser", "--profile", "unattended_7_day")
    assert rc == 0 and "ALL CHECKS PASSED" in out, out[-2500:]
    assert "[PASS] research progress" in out and "[PASS] process guardian3" in out and "[PASS] dashboard responds" in out
    c = rt.conn()
    assert db.get_state(c, "profile") == "unattended_7_day" and db.settings(c)["mem_pause_pct"] == 80
    sup = int(db.get_state(c, "supervisor_pid"))
    rc2, out2 = run("launch", "--no-browser")
    assert rc2 == 0 and "not starting a second one" in out2 and int(db.get_state(rt.conn(), "supervisor_pid")) == sup
    rc3, out3 = run("check")
    assert rc3 == 0, out3[-1500:]
    assert sysinfo.pid_alive(sup)                                   # launcher processes have exited; the runtime has not
    rc4, out4 = run("shutdown")
    assert rc4 == 0 and "exited cleanly" in out4
    rc5, out5 = run("check")
    assert rc5 == 1 and "FAIL" in out5                              # a stopped runtime is reported as failing, never healthy
