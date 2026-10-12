"""Durable job queue on SQLite. Leases + heartbeats make crashed workers recoverable; attempts are bounded."""
import json, time
from . import db, sysinfo


def enqueue(conn, type_, payload, priority=5, dedupe=None, max_attempts=3, not_before=0.0):
    try:
        cur = conn.execute("INSERT OR IGNORE INTO jobs(type,payload,status,priority,max_attempts,not_before,created_at,dedupe_key) "
                           "VALUES(?,?, 'pending', ?, ?, ?, ?, ?)",
                           (type_, json.dumps(payload), priority, max_attempts, not_before, time.time(), dedupe))
        return cur.lastrowid if cur.rowcount else None
    except Exception:
        raise


def claim(conn, owner, pid, types=None):
    now = time.time()
    q = "SELECT id FROM jobs WHERE status='pending' AND not_before<=?"
    args = [now]
    if types:
        q += " AND type IN (%s)" % ",".join("?" * len(types))
        args += list(types)
    q += " ORDER BY priority, id LIMIT 1"
    with db.tx(conn):
        r = conn.execute(q, args).fetchone()
        if not r:
            return None
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, lease_owner=?, lease_pid=?, heartbeat=?, started_at=?, "
                     "progress=? WHERE id=?", (owner, pid, now, now, json.dumps({"msg": "started", "ts": now}), r[0]))
    return conn.execute("SELECT * FROM jobs WHERE id=?", (r[0],)).fetchone()


def heartbeat(conn, job_id, msg=None):
    now = time.time()
    if msg is None:
        conn.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND status='running'", (now, job_id))
    else:
        conn.execute("UPDATE jobs SET heartbeat=?, progress=? WHERE id=? AND status='running'",
                     (now, json.dumps({"msg": msg, "ts": now}), job_id))


def complete(conn, job_id, result=None):
    conn.execute("UPDATE jobs SET status='done', finished_at=?, result=?, lease_owner=NULL WHERE id=?",
                 (time.time(), json.dumps(result) if result is not None else None, job_id))


def fail(conn, job_id, error, retryable=True):
    r = conn.execute("SELECT attempts,max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    if r is None:
        return "gone"
    if retryable and r["attempts"] < r["max_attempts"]:
        delay = min(3600, 5 * 2 ** r["attempts"])          # exponential backoff
        conn.execute("UPDATE jobs SET status='pending', error=?, not_before=?, lease_owner=NULL WHERE id=?",
                     (error[:2000], time.time() + delay, job_id))
        return "retry"
    conn.execute("UPDATE jobs SET status='quarantined', error=?, finished_at=?, lease_owner=NULL WHERE id=?",
                 (error[:2000], time.time(), job_id))
    return "quarantined"


def reclaim_stale(conn, heartbeat_timeout=90.0, progress_timeout=None):
    """Requeue jobs whose worker is dead or silent. A job with a LIVE worker and fresh heartbeat is never touched
    merely for being slow; 'stuck but heartbeating' is judged on progress timestamps (progress_timeout)."""
    now = time.time()
    out = []
    for j in conn.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
        dead = not sysinfo.pid_alive(j["lease_pid"])
        silent = now - (j["heartbeat"] or 0) > heartbeat_timeout
        stuck = False
        if progress_timeout:
            try:
                stuck = now - json.loads(j["progress"])["ts"] > progress_timeout
            except Exception:
                stuck = False
        if dead or silent or stuck:
            why = "worker process dead" if dead else ("heartbeat silent" if silent else "no progress")
            with db.tx(conn):
                res = fail(conn, j["id"], f"reclaimed: {why}")
            out.append((j["id"], j["type"], why, res))
    return out


def counts(conn):
    d = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")}
    return {k: d.get(k, 0) for k in ("pending", "running", "done", "quarantined")}
