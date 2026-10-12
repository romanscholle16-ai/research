"""Writes PROJECT_STATE.json (no secrets): phases, tests, runtime state, last checkpoint - lets a new session resume."""
import json, pathlib, subprocess, time
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
          "live_trading_enabled": False}
    p = ROOT / "PROJECT_STATE.json"
    p.write_text(json.dumps(st, indent=1, default=str))
    return str(p)
