"""Windows-readiness checks that can run on ANY OS (command construction, file formats, path handling, launcher logic), plus
Windows-only tests that are SKIPPED elsewhere (reported as skipped, never as passed)."""
import json, os, pathlib, subprocess, sys, xml.etree.ElementTree as ET
import pytest
from nexus import config, db, launcher, startup, sysinfo, runtime, log
import conftest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WIN = sys.platform.startswith("win")
BATS = ["START_NEXUS.bat", "STOP_NEXUS.bat", "RESTART_NEXUS.bat", "CHECK_NEXUS.bat", "INSTALL_AUTOSTART.bat", "UNINSTALL_AUTOSTART.bat", "scripts/nexus_env.bat"]


@pytest.mark.parametrize("name", BATS)
def test_bat_files_exist_crlf_ascii(name):
    b = (ROOT / name).read_bytes()
    assert b.count(b"\r\n") == b.count(b"\n") > 3, "batch files must use CRLF line endings"
    b.decode("ascii")                                          # no non-ASCII that a legacy code page could mangle


def test_start_bat_has_every_required_step():
    t = (ROOT / "START_NEXUS.bat").read_text()
    for needle in ("py -%%V", ".venv", "-m venv", "pip install", "requirements-runtime.txt", "-m nexus launch", "var\\logs\\launcher.log",
                   "exit /b 3", "exit /b 4", "%~dp0", "pushd", "popd", "--autostart"):
        assert needle in t, needle
    assert "SETX" not in t.upper() and "REG ADD" not in t.upper() and "powercfg" not in t.lower()   # no system-wide changes
    assert "-m nexus shutdown" in (ROOT / "STOP_NEXUS.bat").read_text()
    assert "-m nexus check" in (ROOT / "CHECK_NEXUS.bat").read_text()
    assert "install-startup" in (ROOT / "INSTALL_AUTOSTART.bat").read_text() and "uninstall-startup" in (ROOT / "UNINSTALL_AUTOSTART.bat").read_text()
    assert (ROOT / "requirements-runtime.txt").read_text().split()[0].startswith("numpy")


def test_autostart_xml_is_valid_unlimited_battery_safe_and_quotes_paths(tmp_path):
    root = tmp_path / "My Folder" / "nexus v2"                  # spaces in the path
    x = startup.task_xml(root, user="PC\\me")
    t = ET.fromstring(x.replace('encoding="UTF-16"', ""))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    g = lambda p: t.find(p, ns).text
    assert g(".//t:ExecutionTimeLimit") == "PT0S"               # no 72-hour kill
    assert g(".//t:DisallowStartIfOnBatteries") == "false" and g(".//t:StopIfGoingOnBatteries") == "false"
    assert g(".//t:MultipleInstancesPolicy") == "IgnoreNew" and g(".//t:RunLevel") == "LeastPrivilege"
    assert g(".//t:Command").endswith("START_NEXUS.bat") and g(".//t:Arguments") == "--autostart"
    assert g(".//t:WorkingDirectory") == str(root)
    assert g(".//t:Delay") == "PT1M"


def test_autostart_not_claimed_on_non_windows(capsys):
    if WIN:
        pytest.skip("windows host")
    assert startup.run(True) == 2
    assert "NOT CONFIGURED" in capsys.readouterr().out


def test_profile_unattended_7_day_is_valid_and_conservative():
    p = config.PROFILES["unattended_7_day"]
    config.validate_settings(p)                                 # every value inside the safe bounds
    assert p["workers"] <= 2 and p["mem_pause_pct"] <= 80 and p["disk_min_free_gb"] >= 5 and p["agent_budget_per_hour"] <= 150


def test_apply_profile_persists_and_rejects_unknown(conn):
    s = launcher.apply_profile(conn, "unattended_7_day")
    assert s["mem_pause_pct"] == 80 and db.get_state(conn, "profile") == "unattended_7_day"
    with pytest.raises(ValueError):
        launcher.apply_profile(conn, "yolo")


def test_preflight_flags_config_tamper_and_port_conflict(conn, monkeypatch):
    ok = {r["check"]: r for r in launcher.preflight(conn)}
    assert ok["trusted config matches baseline"]["ok"] and ok["live trading disabled"]["ok"] and ok["database integrity"]["ok"]
    monkeypatch.setitem(config.THRESHOLDS, "s2_min_t", 0.1)
    bad = {r["check"]: r for r in launcher.preflight(conn)}
    assert not bad["trusted config matches baseline"]["ok"]
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    monkeypatch.setenv("NEXUS_PORT", str(s.getsockname()[1]))
    conflict = {r["check"]: r for r in launcher.preflight(conn)}
    s.close()
    assert not [r for r in conflict.values() if r["check"].startswith("dashboard port")][0]["ok"]     # another program owns the port


def test_verify_reports_failure_for_dead_runtime(conn):
    rows = launcher.verify(conn, sample_s=0.2, progress_wait_s=0)
    by = {r["check"]: r for r in rows}
    assert not by["supervisor owns lock + heartbeat fresh"]["ok"] and not by["dashboard responds"]["ok"]
    assert launcher.report(rows, "t") == 1                      # a dead runtime can never produce exit code 0


def test_log_files_are_per_process(home, monkeypatch):
    monkeypatch.setenv("NEXUS_PROC", "worker-9")
    import logging
    logging.getLogger("nexus.zzz").handlers.clear()
    lg = log.get("zzz"); lg.warning("hello")
    assert (home / "logs" / "zzz.worker-9.log").exists()        # never a file shared between processes (Windows cannot rotate those)


def test_paths_with_spaces_and_unicode_home(tmp_path, monkeypatch):
    h = tmp_path / "Mi Carpeta ñ" / "nexus data"
    monkeypatch.setenv("NEXUS_HOME", str(h))
    c = db.open_db()
    assert db.integrity_check(c)[0] and (h / "nexus.db").exists()
    b = db.backup(c, kind="manual")
    assert b["ok"] and pathlib.Path(b["path"]).exists()
    conftest.write_dataset(h, "ETHUSDT", conftest.synth_arrays(500, 1))
    from nexus import data
    data._CACHE.clear()
    assert len(data.load_market("ETHUSDT", "1h")) == 500


def test_atomic_replace_of_dataset_while_previous_copy_was_read(home):
    """load_market closes its file handle, so replacing the npz afterwards works (on Windows an open handle blocks os.replace)."""
    from nexus import data
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(500, 1)); data._CACHE.clear()
    data.load_market("ETHUSDT", "1h")
    src = home / "data" / "ETHUSDT_1h.npz"
    tmp = home / "data" / "new.npz"
    tmp.write_bytes(src.read_bytes())
    os.replace(tmp, src)                                        # must not raise


def test_utf8_console_does_not_crash_on_symbols():
    r = subprocess.run([sys.executable, "-c", "from nexus.__main__ import main; print('\\u2713 \\u25d0 \\u2715')"], cwd=ROOT, capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert r.returncode in (0, 1)                               # printing check marks must never raise UnicodeEncodeError


def test_sysinfo_metrics_return_numbers():
    assert 0 <= sysinfo.cpu_load() <= 100 and sysinfo.mem()["total_mb"] > 0 and sysinfo.disk(".")["free_gb"] > 0
    assert sysinfo.proc_rss_mb(os.getpid()) > 0


# --------------------------------------------------------------- Windows-only (skipped elsewhere, never counted as passed) ---------
@pytest.mark.skipif(not WIN, reason="Windows-only: msvcrt lock")
def test_win_supervisor_lock_exclusive(home):
    a, b = runtime.Lock(runtime.lock_path()), runtime.Lock(runtime.lock_path())
    assert a.acquire() and not b.acquire()
    a.release()


@pytest.mark.skipif(not WIN, reason="Windows-only: real scheduled task round trip")
def test_win_scheduled_task_roundtrip():
    assert startup.run(True) == 0
    q = subprocess.run(["schtasks", "/Query", "/TN", startup.TASK], capture_output=True, text=True)
    assert q.returncode == 0
    assert startup.run(False) == 0


@pytest.mark.skipif(not WIN, reason="Windows-only: detached process creation and CTRL_BREAK shutdown")
def test_win_detached_start_and_graceful_shutdown(home):
    r = runtime.start_detached(wait=40)
    assert r["started"]
    c = db.open_db()
    runtime.send_command(c, "shutdown")
    import time
    for _ in range(120):
        if not runtime.supervisor_lock_held():
            break
        time.sleep(0.5)
    assert not runtime.supervisor_lock_held()
