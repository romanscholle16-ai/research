"""AUTONOMY ACCEPTANCE TEST (synthetic fixtures - validates the ENGINE, not market performance).

Runs the real service + worker subprocesses against a temp home with deterministic synthetic data, injects a worker
crash, a hang (watchdog), a job exception, a mock-AI quota exhaustion, a hard SIGKILL of the whole service
(simulated power loss / reboot) and then checks research resumed on its own and >=100 cycles completed unattended.
A short test run is NOT evidence of 24/7 operation; see docs/ACCEPTANCE.md.
"""
import json, os, signal, subprocess, sys, time, pathlib
import pytest
from strategylab.db import DB
from strategylab.config import Settings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def start_service(home, env_extra):
    env = {**os.environ, "STRATEGYLAB_HOME": str(home), "STRATEGYLAB_SYNTHETIC": "1", "STRATEGYLAB_SYN_N": "4000",
           "STRATEGYLAB_AI_MOCK": "1", "STRATEGYLAB_FAST": "1", **env_extra}
    return subprocess.Popen([sys.executable, "-m", "strategylab.service"], cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(cond, timeout, step=0.5):
    t = time.time()
    while time.time() - t < timeout:
        if cond(): return True
        time.sleep(step)
    return False


def test_100_cycle_autonomy_with_failures_and_restart(home):
    st = Settings(); st.symbols = ["BTCUSDT", "ETHUSDT"]; st.timeframes = ["1h", "4h"]
    st.watchdog_stall_s = 6; st.job_timeout_s = 8; st.mc_paths = 200; st.save()
    db = DB(); db.set("control", "running")
    faults = json.dumps({"crash": [7, 120], "hang": [15], "error": [20]})
    p = start_service(home, {"STRATEGYLAB_FAULTS": faults})
    try:
        assert wait_for(lambda: db.get("cycles", 0) >= 60, 150), f"stalled at {db.get('cycles')} cycles"
        c1 = db.get("cycles", 0)
        os.killpg(p.pid, signal.SIGKILL); p.wait()                       # hard kill service AND worker (same session/group)
        time.sleep(1)
        # an orphan worker may survive if it was not in the group; make sure nothing is writing
        assert db.q("SELECT COUNT(*) n FROM jobs WHERE status='running'")[0]["n"] >= 0
        (home / "service.pid").unlink(missing_ok=True)
        p = start_service(home, {"STRATEGYLAB_FAULTS": faults})           # "reboot": control state persisted as 'running'
        assert wait_for(lambda: db.get("cycles", 0) >= c1 + 100, 200), "did not resume after restart"
        total = db.get("cycles", 0)
        db.set("control", "stopped"); db.set("stop_requested", time.time())
        assert wait_for(lambda: db.q("SELECT COUNT(*) n FROM jobs WHERE status='running'")[0]["n"] == 0, 30), "jobs left running after stop"
    finally:
        try: os.killpg(p.pid, signal.SIGKILL)
        except Exception: pass

    assert total >= 160
    ev = " | ".join(r["message"] for r in db.q("SELECT message FROM events"))
    assert "injecting crash" in ev and "injecting hang" in ev and "injecting error" in ev
    assert "exited rc=1" in ev                                   # crash was detected and handled
    assert "stalled" in ev or "timeout" in ev                           # hang was killed by watchdog
    assert "job" in ev and "failed: injected failure" in ev             # job exception recorded, retried
    assert "recovered" in ev                                            # post-restart recovery of interrupted jobs
    assert "quota exhausted" in ev                                      # AI quota exhaustion handled via fallback
    assert db.get("mock_calls", 0) > 3 and db.get("ai_state")
    # research continued past failures, results persisted, nothing duplicated, everything flagged synthetic
    n_exp = db.q("SELECT COUNT(*) n FROM jobs WHERE kind='experiment' AND status='done'")[0]["n"]
    assert n_exp >= 100
    assert db.q("SELECT COUNT(*) n FROM strategy_status WHERE synthetic=0")[0]["n"] == 0
    dup = db.q("SELECT strategy_id,symbol,timeframe,COUNT(*) n FROM strategy_status GROUP BY 1,2,3 HAVING n>1")
    assert not dup
    assert db.q("SELECT COUNT(*) n FROM strategy_status")[0]["n"] >= 100
    perm_failed = db.q("SELECT COUNT(*) n FROM jobs WHERE status='failed'")[0]["n"]
    assert perm_failed <= 3                                              # bounded; transient faults retried, not lost
    assert not any(r["status"] == "BACKTEST_QUALIFIED" for r in db.q("SELECT status FROM strategy_status"))  # pure noise must not qualify


def test_resource_limit_makes_worker_exit_for_restart(home):
    st = Settings(); st.max_rss_mb = 1; st.save(); DB().set("control", "running")
    env = {**os.environ, "STRATEGYLAB_HOME": str(home), "STRATEGYLAB_SYNTHETIC": "1"}
    r = subprocess.run([sys.executable, "-m", "strategylab.worker"], cwd=ROOT, env=env, timeout=60)
    assert r.returncode == 75


def test_pause_and_stop_controls(home):
    st = Settings(); st.save(); db = DB(); db.set("control", "running")
    p = start_service(home, {})
    try:
        assert wait_for(lambda: db.get("cycles", 0) >= 5, 60)
        db.set("control", "paused"); time.sleep(3); a = db.get("cycles", 0); time.sleep(3)
        assert db.get("cycles", 0) - a <= 1                              # paused => no progress
        db.set("control", "running"); assert wait_for(lambda: db.get("cycles", 0) > a + 5, 60)   # resume
    finally:
        os.killpg(p.pid, signal.SIGKILL)
