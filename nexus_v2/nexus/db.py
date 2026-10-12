"""SQLite (WAL) persistence: migrations, transactions, integrity checks, backup/restore, event helpers."""
import contextlib, json, os, shutil, sqlite3, time

from . import config

MIGRATIONS = {
1: """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE datasets(id INTEGER PRIMARY KEY, symbol TEXT, kind TEXT, timeframe TEXT, source TEXT, retrieved_at REAL,
  rows INTEGER, start_ts INTEGER, end_ts INTEGER, gaps INTEGER, dupes INTEGER, bad_rows INTEGER, checksum TEXT,
  version TEXT, data_class TEXT, path TEXT, manifest_json TEXT);
CREATE TABLE data_files(path TEXT PRIMARY KEY, symbol TEXT, kind TEXT, period TEXT, sha256 TEXT, verified INTEGER,
  size INTEGER, downloaded_at REAL);
CREATE TABLE data_splits(key TEXT PRIMARY KEY, start_ts INTEGER, explore_end_ts INTEGER, dev_end_ts INTEGER,
  holdout_end_ts INTEGER, created_at REAL, dataset_version TEXT);
CREATE TABLE strategies(id TEXT PRIMARY KEY, family TEXT, agent TEXT, symbol TEXT, timeframe TEXT, spec_json TEXT,
  parent_id TEXT, created_at REAL);
CREATE TABLE hypotheses(id INTEGER PRIMARY KEY, agent TEXT, family TEXT, symbol TEXT, timeframe TEXT, text TEXT,
  rationale TEXT, falsifier TEXT, status TEXT, n_experiments INTEGER DEFAULT 0, created_at REAL, updated_at REAL,
  UNIQUE(agent,family,symbol,timeframe));
CREATE TABLE experiments(id INTEGER PRIMARY KEY, strategy_id TEXT, hypothesis_id INTEGER, agent TEXT, family TEXT,
  dataset_version TEXT, engine_version TEXT, phase TEXT, stage TEXT, status TEXT, failure_reason TEXT, seed INTEGER,
  created_at REAL, started_at REAL, completed_at REAL, runtime_ms REAL, trades INTEGER, gross REAL, net REAL,
  fees REAL, funding REAL, slippage REAL, expectancy_r REAL, exp_usd REAL, t_stat REAL, pf REAL, win_rate REAL,
  max_dd REAL, metrics_json TEXT, lessons TEXT, next_suggestion TEXT, why_selected TEXT, job_id INTEGER,
  UNIQUE(strategy_id, dataset_version, engine_version, phase));
CREATE INDEX ix_exp_family ON experiments(family, status);
CREATE INDEX ix_exp_agent_time ON experiments(agent, completed_at);
CREATE TABLE backtest_runs(id INTEGER PRIMARY KEY, kind TEXT, strategy_id TEXT, experiment_id INTEGER, trades INTEGER,
  created_at REAL);
CREATE TABLE candidates(strategy_id TEXT PRIMARY KEY, stage TEXT, status TEXT, promoted_at REAL, updated_at REAL,
  dev_exp_r REAL, dev_t REAL, dev_trades INTEGER, blockers_json TEXT, integrity_verified INTEGER DEFAULT 0,
  integrity_checked_at REAL, paper_enrolled_ts INTEGER, summary_json TEXT);
CREATE TABLE validations(id INTEGER PRIMARY KEY, strategy_id TEXT, stage TEXT, passed INTEGER, n_trades INTEGER,
  metrics_json TEXT, thresholds_hash TEXT, engine_version TEXT, dataset_version TEXT, created_at REAL);
CREATE INDEX ix_val ON validations(strategy_id, stage);
CREATE TABLE candidate_trades(id INTEGER PRIMARY KEY, strategy_id TEXT, phase TEXT, entry_ts INTEGER, exit_ts INTEGER,
  side INTEGER, entry_px REAL, exit_px REAL, qty REAL, net REAL, fees REAL, funding REAL, slippage REAL, r REAL,
  reason TEXT);
CREATE INDEX ix_ct ON candidate_trades(strategy_id, phase);
CREATE TABLE holdout_access(id INTEGER PRIMARY KEY, strategy_id TEXT, ts REAL, purpose TEXT, caller TEXT,
  dataset_version TEXT, passed INTEGER);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, type TEXT, payload TEXT, status TEXT, priority INTEGER DEFAULT 5,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, lease_owner TEXT, lease_pid INTEGER, heartbeat REAL,
  progress TEXT, not_before REAL DEFAULT 0, created_at REAL, started_at REAL, finished_at REAL, error TEXT,
  dedupe_key TEXT UNIQUE, result TEXT);
CREATE INDEX ix_jobs_claim ON jobs(status, priority, not_before);
CREATE TABLE processes(name TEXT PRIMARY KEY, role TEXT, pid INTEGER, started_at REAL, heartbeat REAL, status TEXT,
  restarts INTEGER DEFAULT 0, last_exit TEXT, info_json TEXT);
CREATE TABLE commands(id INTEGER PRIMARY KEY, ts REAL, command TEXT, args TEXT, status TEXT, result TEXT);
CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE incidents(id INTEGER PRIMARY KEY, detected_at REAL, component TEXT, severity TEXT, error TEXT,
  state_json TEXT, likely_cause TEXT, recovery TEXT, recovery_result TEXT, related_logs TEXT, recurred INTEGER DEFAULT 0,
  degraded INTEGER DEFAULT 1, resolved_at REAL, fingerprint TEXT);
CREATE INDEX ix_inc ON incidents(fingerprint, resolved_at);
CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, type TEXT, component TEXT, message TEXT, data_json TEXT);
CREATE INDEX ix_events_ts ON events(ts);
CREATE TABLE guardian_log(id INTEGER PRIMARY KEY, ts REAL, guardian TEXT, check_name TEXT, ok INTEGER, detail TEXT,
  action TEXT);
CREATE TABLE scheduler_log(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, family TEXT, mode TEXT, why TEXT,
  would_change TEXT, strategy_id TEXT);
CREATE TABLE agent_state(agent TEXT PRIMARY KEY, state_json TEXT, updated_at REAL);
CREATE TABLE family_weights(family TEXT PRIMARY KEY, weight REAL, reason TEXT, updated_at REAL);
CREATE TABLE stagnation_events(id INTEGER PRIMARY KEY, ts REAL, window_s INTEGER, diagnosis TEXT, action TEXT);
CREATE TABLE checkpoints(id INTEGER PRIMARY KEY, ts REAL, path TEXT, kind TEXT, size INTEGER, integrity_ok INTEGER);
CREATE TABLE exchange_specs(id INTEGER PRIMARY KEY, exchange TEXT, symbol TEXT, field TEXT, value TEXT, status TEXT,
  source TEXT, verified_at REAL, note TEXT, UNIQUE(exchange,symbol,field));
CREATE TABLE test_evidence(id INTEGER PRIMARY KEY, ts REAL, suite TEXT, passed INTEGER, failed INTEGER, skipped INTEGER,
  output TEXT);
CREATE TABLE metrics_hist(id INTEGER PRIMARY KEY, ts REAL, data_json TEXT);
""",
}


def connect(path=None, timeout=30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def tx(conn):
    """Write transaction. BEGIN IMMEDIATE avoids upgrade deadlocks between workers."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn) -> int:
    cur = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in sorted(MIGRATIONS):
        if v > cur:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in MIGRATIONS[v].split(";\n"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={v}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_db(path=None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    init_meta(conn)
    return conn


def init_meta(conn):
    """Record immutable baselines once: project start date and trusted-config hash."""
    if conn.execute("SELECT 1 FROM meta WHERE key='project_start'").fetchone():
        return
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO meta VALUES('project_start',?)", (str(time.time()),))
        conn.execute("INSERT OR IGNORE INTO meta VALUES('trusted_hash_baseline',?)", (config.trusted_hash(),))
        conn.execute("INSERT OR IGNORE INTO meta VALUES('trusted_config_json',?)",
                     (json.dumps(config.trusted_config(), default=str),))
        conn.execute("INSERT OR IGNORE INTO runtime_state VALUES('desired','stopped',?)", (time.time(),))
        conn.execute("INSERT OR IGNORE INTO runtime_state VALUES('settings',?,?)",
                     (json.dumps(config.default_settings()), time.time()))


def rebaseline(conn):
    """Re-record the trusted-config baseline. Only permitted while NO research exists (fresh install / pre-launch);
    afterwards the baseline can only change through the controlled-change process."""
    if conn.execute("SELECT (SELECT COUNT(*) FROM experiments)+(SELECT COUNT(*) FROM validations)").fetchone()[0]:
        raise PermissionError("research evidence exists; baseline is frozen")
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta VALUES('trusted_hash_baseline',?)", (config.trusted_hash(),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('trusted_config_json',?)", (json.dumps(config.trusted_config(), default=str),))
        conn.execute("DELETE FROM meta WHERE key='trusted_code_hash_baseline'")
        event(conn, "change_control", "db", "trusted baseline re-recorded (pre-launch)")


def integrity_check(conn, full=True):
    rows = conn.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchall()
    msgs = [r[0] for r in rows]
    return msgs == ["ok"], msgs


def backup(conn, dest_dir=None, kind="periodic") -> dict:
    d = config.path("backups", "x", mkdir=True).parent if dest_dir is None else __import__("pathlib").Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"nexus-{int(time.time()*1000)}-{kind}.db"
    dst = sqlite3.connect(str(dest))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    chk = sqlite3.connect(str(dest))
    ok, msgs = integrity_check(chk, True)
    chk.close()
    size = dest.stat().st_size
    with tx(conn):
        conn.execute("INSERT INTO checkpoints(ts,path,kind,size,integrity_ok) VALUES(?,?,?,?,?)",
                     (time.time(), str(dest), kind, size, int(ok)))
    # keep newest 6 periodic backups (the DB grows with the campaign)
    olds = sorted(d.glob("nexus-*-periodic.db"))[:-6]
    for o in olds:
        with contextlib.suppress(OSError):
            o.unlink()
    return {"path": str(dest), "ok": ok, "size": size}


def restore(backup_path, dest_path=None):
    """Restore a backup over the DB file (callers must stop the runtime first). Current DB is kept aside."""
    dest = __import__("pathlib").Path(dest_path or config.db_path())
    src = sqlite3.connect(str(backup_path))
    ok, msgs = integrity_check(src, True)
    src.close()
    if not ok:
        raise RuntimeError(f"backup failed integrity check: {msgs[:3]}")
    if dest.exists():
        shutil.copy2(dest, str(dest) + f".pre-restore-{int(time.time())}")
    for ext in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(str(dest) + ext)
    shutil.copy2(backup_path, dest)


# ---- small helpers -----------------------------------------------------------------------------
def event(conn, type_, component, message, data=None):
    conn.execute("INSERT INTO events(ts,type,component,message,data_json) VALUES(?,?,?,?,?)",
                 (time.time(), type_, component, message[:2000], json.dumps(data) if data is not None else None))


def get_state(conn, key, default=None):
    r = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_state(conn, key, value):
    conn.execute("INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value, updated_at=excluded.updated_at", (key, str(value), time.time()))


def settings(conn) -> dict:
    s = config.default_settings()
    r = get_state(conn, "settings")
    if r:
        s.update(json.loads(r))
    return s


def incident(conn, component, severity, error, likely_cause="", recovery="", result="", state=None,
             fingerprint=None, degraded=True):
    """Open (or mark recurrence of) an incident. Returns incident id."""
    fp = fingerprint or f"{component}:{error[:80]}"
    row = conn.execute("SELECT id FROM incidents WHERE fingerprint=? AND resolved_at IS NULL", (fp,)).fetchone()
    if row:
        conn.execute("UPDATE incidents SET recurred=recurred+1, recovery=COALESCE(NULLIF(?,''),recovery), "
                     "recovery_result=COALESCE(NULLIF(?,''),recovery_result) WHERE id=?", (recovery, result, row[0]))
        return row[0]
    cur = conn.execute("INSERT INTO incidents(detected_at,component,severity,error,state_json,likely_cause,recovery,"
                       "recovery_result,related_logs,fingerprint,degraded) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), component, severity, error[:4000], json.dumps(state, default=str) if state else None,
                        likely_cause, recovery, result, str(config.path("logs", "x.log").parent), fp, int(degraded)))
    event(conn, "incident", component, f"[{severity}] {error[:300]}")
    return cur.lastrowid


def resolve_incident(conn, fingerprint, result="recovered"):
    conn.execute("UPDATE incidents SET resolved_at=?, degraded=0, recovery_result=? WHERE fingerprint=? AND "
                 "resolved_at IS NULL", (time.time(), result, fingerprint))
