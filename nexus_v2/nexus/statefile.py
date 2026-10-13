"""Writes PROJECT_STATE.json (no secrets): phases, tests, runtime state, last checkpoint - lets a new session resume."""
import json, pathlib, platform, subprocess, sys, time
from . import config, db, memory, runtime, __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]


def write(conn):
    h = runtime.health(conn)
    ck = conn.execute("SELECT ts,path,integrity_ok FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
    te = conn.execute("SELECT ts,suite,passed,failed,skipped FROM test_evidence ORDER BY id DESC LIMIT 5").fetchall()
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = ""
    st = {"version": __version__, "config_version": config.CONFIG_VERSION, "trusted_config_hash": config.trusted_hash(), "git_commit": commit,
          "written_at": time.time(), "runtime": {k: h[k] for k in ("status", "reasons", "desired", "supervisor_alive", "workers_alive", "queue")},
          "counters": memory.counters(conn), "last_checkpoint": dict(ck) if ck else None, "recent_test_evidence": [dict(r) for r in te],
          "live_trading_enabled": False,
          "environment": {"platform": sys.platform, "os": platform.platform(), "python": sys.version.split()[0], "install_path": str(ROOT),
                          "is_windows": sys.platform.startswith("win"), "remote_session": bool(__import__("os").environ.get("CLAUDE_CODE_REMOTE"))},
          "profile": db.get_state(conn, "profile"),
          "resume_instructions": ["git status; read docs/WINDOWS_DEPLOYMENT_REPORT.md and docs/UNATTENDED_READINESS.md",
                                  "On the Windows PC: START_NEXUS.bat, then CHECK_NEXUS.bat, then INSTALL_AUTOSTART.bat",
                                  "python -m nexus test  (Windows-only tests run there and must be reported separately)",
                                  "record the soak in docs/SOAK_LOG.md only from real evidence"]}
    if (ROOT / "docs" / "READINESS.json").exists():
        st["readiness"] = json.loads((ROOT / "docs" / "READINESS.json").read_text())
    p = ROOT / "project_state.json"
    p.write_text(json.dumps(st, indent=1, default=str))
    return str(p)
