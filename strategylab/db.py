"""Persistent research memory (SQLite, WAL). All writes are short transactions => crash safe."""
from __future__ import annotations
import json, sqlite3, threading, time
from pathlib import Path
from .config import home_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS hypotheses(
  id INTEGER PRIMARY KEY, created REAL, family TEXT, text TEXT, rationale TEXT,
  falsification TEXT, source TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS strategies(
  id TEXT PRIMARY KEY, created REAL, hypothesis_id INTEGER, parent_id TEXT, family TEXT,
  spec_json TEXT, spec_hash TEXT UNIQUE, version INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS experiments(
  id INTEGER PRIMARY KEY, strategy_id TEXT, stage TEXT, symbol TEXT, timeframe TEXT,
  dataset_id TEXT, started REAL, finished REAL, status TEXT, metrics_json TEXT,
  failure_reasons TEXT, synthetic INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_exp_strat ON experiments(strategy_id);
CREATE TABLE IF NOT EXISTS strategy_status(
  strategy_id TEXT, symbol TEXT, timeframe TEXT, status TEXT, updated REAL,
  summary_json TEXT, synthetic INTEGER DEFAULT 0, PRIMARY KEY(strategy_id,symbol,timeframe));
CREATE TABLE IF NOT EXISTS trades(
  strategy_id TEXT, symbol TEXT, timeframe TEXT, stage TEXT, trades_json TEXT,
  PRIMARY KEY(strategy_id,symbol,timeframe,stage));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, priority INTEGER DEFAULT 5,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, not_before REAL DEFAULT 0,
  created REAL, started REAL, finished REAL, heartbeat REAL, error TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS ix_jobs ON jobs(status, priority, not_before);
CREATE TABLE IF NOT EXISTS datasets(
  id TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT, source TEXT, start_ts INTEGER, end_ts INTEGER,
  rows INTEGER, gaps INTEGER, has_funding INTEGER, has_taker INTEGER, synthetic INTEGER, notes TEXT, created REAL);
CREATE TABLE IF NOT EXISTS ai_usage(
  id INTEGER PRIMARY KEY, ts REAL, provider TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER,
  cost_usd REAL, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL, level TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS signal_fp(
  symbol TEXT, timeframe TEXT, fp TEXT, family TEXT, n_tested INTEGER DEFAULT 0, n_failed INTEGER DEFAULT 0, PRIMARY KEY(symbol,timeframe,fp));
CREATE TABLE IF NOT EXISTS hyp_registry(
  hkey TEXT PRIMARY KEY, entry_key TEXT, group_key TEXT, family TEXT, symbol TEXT, timeframe TEXT, side TEXT, params_json TEXT,
  n_variants INTEGER DEFAULT 0, n_failed INTEGER DEFAULT 0, best_stage INTEGER DEFAULT 0, first_ts REAL, last_ts REAL);
CREATE INDEX IF NOT EXISTS ix_hyp_group ON hyp_registry(group_key);
CREATE TABLE IF NOT EXISTS precheck_rejections(id INTEGER PRIMARY KEY, ts REAL, family TEXT, symbol TEXT, timeframe TEXT, reason TEXT, hkey TEXT);
CREATE TABLE IF NOT EXISTS director_batches(id INTEGER PRIMARY KEY, ts REAL, mode TEXT, n INTEGER, composition_json TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS research_metrics(id INTEGER PRIMARY KEY, ts REAL, body TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY, ts REAL, family TEXT, symbol TEXT, timeframe TEXT, reasons TEXT, strategy_id TEXT);
"""


class DB:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or (home_dir() / "research.db"))
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    # --- kv / control
    def get(self, k, default=None):
        r = self.conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.conn().execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def log(self, level, source, message):
        self.conn().execute("INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)", (time.time(), level, source, str(message)[:2000]))

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        return self.conn().execute(sql, args)

    # --- jobs
    def enqueue(self, kind, payload=None, priority=5, not_before=0.0):
        cur = self.x("INSERT INTO jobs(kind,payload,priority,not_before,created) VALUES(?,?,?,?,?)",
                     (kind, json.dumps(payload or {}), priority, not_before, time.time()))
        return cur.lastrowid

    def claim_job(self, kinds=None, exclude=()):
        """Atomically claim the highest-priority runnable job."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            ex = "".join(f" AND kind!='{k}'" for k in exclude if k.isalpha())
            r = c.execute(f"SELECT * FROM jobs WHERE status='queued' AND not_before<=?{ex} ORDER BY priority, id LIMIT 1", (now,)).fetchone()
            if not r:
                c.execute("COMMIT"); return None
            c.execute("UPDATE jobs SET status='running', started=?, heartbeat=?, attempts=attempts+1 WHERE id=?", (now, now, r["id"]))
            c.execute("COMMIT")
            d = dict(r); d["attempts"] += 1; d["payload"] = json.loads(d["payload"] or "{}")
            return d
        except Exception:
            c.execute("ROLLBACK"); raise

    def heartbeat(self, job_id=None):
        now = time.time()
        self.set("worker_heartbeat", now)
        if job_id:
            self.x("UPDATE jobs SET heartbeat=? WHERE id=?", (now, job_id))

    def finish_job(self, job_id, result=None):
        self.x("UPDATE jobs SET status='done', finished=?, result=? WHERE id=?", (time.time(), json.dumps(result or {}, default=str)[:4000], job_id))

    def fail_job(self, job_id, error, max_attempts=3, base_backoff=2.0):
        r = self.x("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        a = r["attempts"] if r else max_attempts
        if a >= max_attempts:
            self.x("UPDATE jobs SET status='failed', finished=?, error=? WHERE id=?", (time.time(), str(error)[:2000], job_id))
        else:
            self.x("UPDATE jobs SET status='queued', error=?, not_before=? WHERE id=?",
                   (str(error)[:2000], time.time() + base_backoff * 2 ** (a - 1), job_id))

    def reclaim_stalled(self, stall_s):
        """Jobs 'running' with stale heartbeat => requeue (bounded by attempts)."""
        cutoff = time.time() - stall_s
        rows = self.q("SELECT id FROM jobs WHERE status='running' AND heartbeat<?", (cutoff,))
        for r in rows:
            self.fail_job(r["id"], "stalled/crashed worker (heartbeat timeout)")
        return len(rows)

    def reclaim_all_running(self):
        rows = self.q("SELECT id FROM jobs WHERE status='running'")
        for r in rows:
            self.fail_job(r["id"], "interrupted by restart", base_backoff=0.1)
        return len(rows)

    # --- counts
    def counts(self, synthetic=None):
        w = "" if synthetic is None else f" WHERE synthetic={int(synthetic)}"
        rows = self.q(f"SELECT status, COUNT(*) n FROM strategy_status{w} GROUP BY status")
        out = {r["status"]: r["n"] for r in rows}
        out["experiments"] = self.q(f"SELECT COUNT(*) n FROM experiments{w}")[0]["n"]
        return out
