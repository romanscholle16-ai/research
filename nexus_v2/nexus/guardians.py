"""Three independent guardians. They supervise; they never generate strategies, never touch financial goals,
thresholds or live-trading switches, and every action is logged and reversible."""
import hashlib, json, os, pathlib, random, shutil, subprocess, sys, time

import numpy as np

from . import config, data, db, jobs, memory, qualify, research as R, strategies as S, sysinfo, log, ENGINE_VERSION

TRUSTED_FILES = ["engine.py", "metrics.py", "qualify.py", "validation.py", "config.py", "data.py", "indicators.py"]


def trusted_code_hash():
    base = pathlib.Path(__file__).parent
    h = hashlib.sha256()
    for f in TRUSTED_FILES:
        h.update(f.encode()); h.update((base / f).read_bytes())
    return h.hexdigest()


def approve_code_baseline(conn, reason, test_summary):
    """The ONLY sanctioned way to change the trusted-code baseline: requires a reason and recorded passing test evidence."""
    if not test_summary or test_summary.get("failed", 1) != 0:
        raise PermissionError("controlled change requires a passing test run")
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta VALUES('trusted_code_hash_baseline',?)", (trusted_code_hash(),))
        db.event(conn, "change_control", "guardian3", f"trusted code baseline updated: {reason}", test_summary)


def approve_config_baseline(conn, reason, test_summary):
    """Controlled change of the trusted CONFIG (thresholds/costs/goals). Requires a reason and a passing test run; the old and new
    hashes and the exact differences are logged. Evidence recorded under the old thresholds automatically becomes stale (re-validated;
    holdout looks already spent stay spent)."""
    if not test_summary or test_summary.get("failed", 1) != 0:
        raise PermissionError("controlled change requires a passing test run")
    old = conn.execute("SELECT value FROM meta WHERE key='trusted_hash_baseline'").fetchone()[0]
    old_cfg = json.loads(conn.execute("SELECT value FROM meta WHERE key='trusted_config_json'").fetchone()[0])
    new_cfg = json.loads(json.dumps(config.trusted_config(), default=str))
    diff = {}
    for sec in new_cfg:
        if isinstance(new_cfg[sec], dict):
            for k, v in new_cfg[sec].items():
                if old_cfg.get(sec, {}).get(k) != v:
                    diff[f"{sec}.{k}"] = {"old": old_cfg.get(sec, {}).get(k), "new": v}
        elif old_cfg.get(sec) != new_cfg[sec]:
            diff[sec] = {"old": old_cfg.get(sec), "new": new_cfg[sec]}
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta VALUES('trusted_hash_baseline',?)", (config.trusted_hash(),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('trusted_config_json',?)", (json.dumps(new_cfg),))
        db.event(conn, "change_control", "guardian3", f"trusted CONFIG changed: {reason}", {"old_hash": old, "new_hash": config.trusted_hash(), "diff": diff, "tests": test_summary})
        db.resolve_incident(conn, "config_changed", "approved via controlled change")
    return diff


class Guardian:
    name = "guardian"
    interval = 10.0

    def __init__(self, conn=None):
        self.conn = conn or db.open_db()
        self.lg = log.get(self.name)
        self.last = {}

    def due(self, key, every):
        now = time.time()
        if now - self.last.get(key, 0) >= every:
            self.last[key] = now
            return True
        return False

    def note(self, check, ok, detail="", action=""):
        self.conn.execute("INSERT INTO guardian_log(ts,guardian,check_name,ok,detail,action) VALUES(?,?,?,?,?,?)",
                          (time.time(), self.name, check, int(ok), detail[:1000], action[:500]))
        if not ok:
            self.lg.warning("%s FAILED: %s -> %s", check, detail, action)

    def raise_incident(self, comp, sev, err, cause="", action="", result="", fp=None, state=None):
        with db.tx(self.conn):
            return db.incident(self.conn, comp, sev, err, cause, action, result, state, fp)

    def clear_incident(self, fp, result="condition cleared"):
        with db.tx(self.conn):
            db.resolve_incident(self.conn, fp, result)

    def cycle(self):
        raise NotImplementedError


# =============================================================================== Guardian 1 ======
class Reliability(Guardian):
    name = "guardian1_reliability"

    def cycle(self):
        c, st = self.conn, db.settings(self.conn)
        now = time.time()
        desired = db.get_state(c, "desired", "stopped")
        # supervisor liveness
        sh = float(db.get_state(c, "supervisor_heartbeat", "0") or 0)
        sup_ok = now - sh < 45
        self.note("supervisor_heartbeat", sup_ok, f"age={now - sh:.0f}s")
        if not sup_ok and desired != "stopped" and db.get_state(c, "shutdown_requested") != "1":
            self.raise_incident("supervisor", "critical", "supervisor heartbeat lost", "supervisor crashed or was killed",
                                "attempting bounded relaunch", fp="supervisor_dead")
            if self.due("relaunch", 120) and not os.environ.get("NEXUS_NO_RELAUNCH"):   # opt-out for tests / externally supervised setups
                from . import runtime
                runtime.start_detached()
                self.note("supervisor_relaunch", True, "relaunch attempted", "launched supervisor (lock prevents duplicates)")
        elif sup_ok:
            self.clear_incident("supervisor_dead", "supervisor heartbeat restored")
        # worker / child heartbeats
        for p in c.execute("SELECT * FROM processes WHERE status IN ('running','starting')").fetchall():
            if p["name"].startswith("guardian1"):
                continue
            age = now - (p["heartbeat"] or 0)
            alive = sysinfo.pid_alive(p["pid"])
            ok = alive and age < 60
            self.note(f"heartbeat:{p['name']}", ok, f"alive={alive} age={age:.0f}s")
            fp = f"proc:{p['name']}"
            if not ok and sup_ok:
                self.raise_incident(p["role"], "error", f"{p['name']} unresponsive (alive={alive}, heartbeat age {age:.0f}s)",
                                    "crash, hang or resource starvation", "supervisor will restart; jobs requeued", fp=fp)
                db.set_state(c, f"restart:{p['name']}", "1")
            elif ok:
                self.clear_incident(fp, "heartbeat restored")
        # duplicate-process check: more live workers than configured means orphans/duplicates
        live_workers = [p["name"] for p in c.execute("SELECT name,pid FROM processes WHERE role='worker' AND status IN ('running','starting')") if sysinfo.pid_alive(p["pid"])]
        dup = len(live_workers) > st["workers"]
        self.note("duplicate_workers", not dup, f"live={live_workers} configured={st['workers']}")
        if dup and sup_ok:
            self.raise_incident("worker", "warning", f"{len(live_workers)} live workers but {st['workers']} configured", "duplicate startup or orphan",
                                "extra workers are retired by the supervisor's reconcile loop", fp="dup_workers", degraded=False)
        elif not dup:
            self.clear_incident("dup_workers")
        # stale jobs
        rc = jobs.reclaim_stale(c, 90.0, st["job_timeout_s"])
        for jid, jt, why, res in rc:
            self.note("job_reclaim", False, f"job {jid} ({jt}): {why}", f"requeued -> {res}")
            self.raise_incident("queue", "warning", f"job {jid} {jt} reclaimed: {why}", why, f"requeue/{res}",
                                fp=f"reclaim:{jt}:{why}")
        q = jobs.counts(c)
        self.note("queue", True, json.dumps(q))
        if q["quarantined"] > 0 and self.due("qwarn", 600):
            self.raise_incident("queue", "warning", f"{q['quarantined']} jobs quarantined after repeated failures",
                                "persistent handler errors", "kept for inspection; excluded from retries", fp="quarantine", degraded=False)
        # database integrity
        if self.due("db_quick", 300):
            ok, msg = db.integrity_check(c, full=False)
            self.note("db_quick_check", ok, "; ".join(msg[:2]))
            if not ok:
                self.raise_incident("database", "critical", f"integrity check failed: {msg[:3]}", "corruption (disk/crash)",
                                    "campaign paused; restore latest verified checkpoint with `nexus restore`", fp="db_integrity")
                db.set_state(c, "desired", "paused")
            else:
                self.clear_incident("db_integrity")
        if self.due("db_full", 3600):
            ok, msg = db.integrity_check(c, full=True)
            self.note("db_full_check", ok, "; ".join(msg[:2]))
        # disk, logs, memory
        d = sysinfo.disk(config.home())
        low = d["free_gb"] < st["disk_min_free_gb"]
        self.note("disk", not low, f"free={d['free_gb']}GB")
        if low:
            self.raise_incident("disk", "critical", f"free disk {d['free_gb']}GB below {st['disk_min_free_gb']}GB", "disk filling",
                                "campaign paused to protect data", fp="disk_low")
            db.set_state(c, "desired", "paused")
        else:
            self.clear_incident("disk_low")
        logs = sum(f.stat().st_size for f in config.path("logs", "x.log").parent.glob("*") if f.is_file()) / 2**20
        self.note("log_size", logs < 500, f"{logs:.0f}MB")
        mem = sysinfo.mem()
        hot = mem["used_pct"] > st["mem_pause_pct"]
        thr = db.get_state(c, "throttle", "none")
        if hot and thr != "high":
            db.set_state(c, "throttle", "high")
            self.raise_incident("resources", "warning", f"memory {mem['used_pct']}% > {st['mem_pause_pct']}%", "memory pressure",
                                "throttled: workers idle until pressure falls", fp="mem_high")
        elif not hot and thr == "high":
            db.set_state(c, "throttle", "none")
            self.clear_incident("mem_high", "pressure relieved; throttle released")
        self.note("memory", not hot, f"{mem['used_pct']}%")
        # data freshness
        r = c.execute("SELECT MAX(end_ts) FROM datasets").fetchone()[0]
        if r:
            age_d = (now * 1000 - r) / 86_400_000
            self.note("data_freshness", age_d < 3, f"newest bar {age_d:.1f} days old")
        # trusted config
        base = c.execute("SELECT value FROM meta WHERE key='trusted_hash_baseline'").fetchone()[0]
        same = base == config.trusted_hash()
        self.note("trusted_config_hash", same, "")
        if not same:
            self.raise_incident("config", "critical", "trusted configuration differs from recorded baseline",
                                "goals/thresholds/costs edited outside change control", "campaign paused", fp="config_changed")
            db.set_state(c, "desired", "paused")
        # checkpoints
        last = c.execute("SELECT MAX(ts) FROM checkpoints").fetchone()[0] or 0
        if now - last > 6 * 3600 and self.due("ckpt", 600):
            res = db.backup(c, kind="periodic")
            self.note("checkpoint", res["ok"], res["path"], "created verified DB backup")
            db.event(c, "checkpoint", "guardian1", f"checkpoint {res['path']} ok={res['ok']}")
        if self.due("metrics_hist", 60):
            with db.tx(c):
                c.execute("INSERT INTO metrics_hist(ts,data_json) VALUES(?,?)", (now, json.dumps({
                    "cpu": sysinfo.cpu_load(), "mem": mem["used_pct"], "disk_free_gb": d["free_gb"], "queue": q["pending"],
                    "done": c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]})))
                c.execute("DELETE FROM metrics_hist WHERE ts<?", (now - 14 * 86400,))
        db.set_state(c, "guardian1_last", now)


# =============================================================================== Guardian 2 ======
class Stagnation(Guardian):
    name = "guardian2_stagnation"
    WINDOWS = [900, 1800, 3600, 10800, 21600, 43200]

    def cycle(self):
        c, now = self.conn, time.time()
        if db.get_state(c, "desired") != "running":
            return
        if not self.due("cycle", 120):
            return
        q = jobs.counts(c)
        procs = c.execute("SELECT COUNT(*) FROM processes WHERE role='worker' AND status='running'").fetchone()[0]
        act = {w: memory.window_activity(c, w) for w in self.WINDOWS}
        db.set_state(c, "activity", json.dumps(act))
        a15 = act[900]
        started = float(db.get_state(c, "running_since", str(now)) or now)
        if now - started < 900:
            return                                  # grace period after (re)start
        # (1) work queued + live workers, but nothing finished -> stuck?
        if q["pending"] > 0 and procs > 0 and a15["jobs_done"] == 0:
            stuck = []
            for j in c.execute("SELECT * FROM jobs WHERE status='running'"):
                prog = json.loads(j["progress"] or "{}").get("ts", 0)
                hb_age = now - (j["heartbeat"] or 0)
                stuck.append((j["id"], round(now - prog), round(hb_age)))
            diag = f"queue={q['pending']} workers={procs} but no job finished in 15m; running jobs (id, progress_age_s, heartbeat_age_s)={stuck[:5]}"
            self.note("no_progress", False, diag)
            action = "none: jobs are heartbeating and within timeout (long-running, not stuck)"
            st = db.settings(c)
            if any(p > st["job_timeout_s"] for _, p, _h in stuck):
                rc = jobs.reclaim_stale(c, 90.0, st["job_timeout_s"])
                action = f"reclaimed stalled jobs {[r[0] for r in rc]}"
            with db.tx(c):
                c.execute("INSERT INTO stagnation_events(ts,window_s,diagnosis,action) VALUES(?,?,?,?)", (now, 900, diag, action))
        # (2) research productivity over longer windows
        for w in (3600, 10800, 43200):
            a = act[w]
            if a["experiments"] >= 200 and a["informative"] == 0:
                diag = f"{a['experiments']} experiments in {w//3600}h but none informative (all invalid/inconclusive)"
                self._redirect(diag, "invalid_or_inconclusive")
                break
        # (3) family concentration and exhaustion
        rows = c.execute("SELECT family,COUNT(*) n FROM experiments WHERE completed_at>? GROUP BY family", (now - 3600,)).fetchall()
        tot = sum(r["n"] for r in rows)
        if tot >= 300:
            for r in rows:
                if r["n"] / tot > 0.4:
                    self._reduce(r["family"], 0.6, f"{r['family']} consumed {r['n']/tot:.0%} of the last hour's experiments")
        if self.due("exhaust", 900):
            self._check_exhaustion()
        # (4) overfit families: many candidates, none surviving walk-forward
        for r in c.execute("""SELECT s.family, COUNT(*) n, SUM(v.passed) p FROM validations v JOIN strategies s ON s.id=v.strategy_id
                              WHERE v.stage='walkforward' GROUP BY s.family HAVING n>=10 AND p=0""").fetchall():
            self._reduce(r["family"], 0.5, f"{r['n']} candidates from {r['family']} all failed walk-forward: in-sample results do not generalise")
        # (5) duplicate pressure
        sl = c.execute("SELECT COUNT(*) FROM scheduler_log WHERE ts>? AND mode='skip'", (now - 3600,)).fetchone()[0]
        self.note("scheduler_skips_1h", sl < 20, f"{sl} agent-ticks with no eligible proposals")
        if sl >= 20:
            self._extend_space()

    def _reduce(self, fam, factor, why):
        c = self.conn
        w = memory.weights(c).get(fam, 1.0)
        if w <= 0.1:
            return
        with db.tx(c):
            memory.set_weight(c, fam, w * factor, "guardian2: " + why)
            c.execute("INSERT INTO stagnation_events(ts,window_s,diagnosis,action) VALUES(?,?,?,?)",
                      (time.time(), 3600, why, f"family weight {w:.2f} -> {max(0.05, w*factor):.2f} (floor 0.05; never zero)"))
            db.event(c, "stagnation", "guardian2", f"redirect budget away from {fam}: {why}")
        self.note("redirect", False, why, f"reduced {fam}")

    def _redirect(self, diag, kind):
        fam = self.conn.execute("SELECT family,SUM(status='inconclusive')*1.0/COUNT(*) f FROM experiments WHERE completed_at>? GROUP BY family "
                                "ORDER BY f DESC LIMIT 1", (time.time() - 10800,)).fetchone()
        if fam:
            self._reduce(fam["family"], 0.5, diag + f" (worst: {fam['family']} {fam['f']:.0%} inconclusive)")

    def _check_exhaustion(self):
        c = self.conn
        counts = {r["family"]: r["n"] for r in c.execute("SELECT family,COUNT(*) n FROM experiments GROUP BY family")}
        for fam, n in counts.items():
            if S.F[fam]["agent"] == "adversarial":
                continue
            space = S.grid_size(fam) * 2 * 3 if not S.F[fam]["other"] else S.grid_size(fam) * 2 * 3
            if n / max(1, space) > 0.9:
                self._reduce(fam, 0.2, f"{fam} space ~{n/space:.0%} explored: diminishing returns")

    def _extend_space(self):
        """Introduce alternative hypotheses: lift weights of the least-explored families (bounded, logged)."""
        c = self.conn
        counts = {r["family"]: r["n"] for r in c.execute("SELECT family,COUNT(*) n FROM experiments GROUP BY family")}
        least = sorted(S.F, key=lambda f: counts.get(f, 0) / S.grid_size(f))[:3]
        with db.tx(c):
            for f in least:
                if S.F[f]["agent"] != "adversarial":
                    memory.set_weight(c, f, min(3.0, memory.weights(c).get(f, 1.0) * 1.5), "guardian2: alternative hypothesis boost (least explored)")
            c.execute("INSERT INTO stagnation_events(ts,window_s,diagnosis,action) VALUES(?,?,?,?)",
                      (time.time(), 3600, "agents repeatedly lack unseen proposals", f"boosted least-explored families {least}"))


# =============================================================================== Guardian 3 ======
class Integrity(Guardian):
    name = "guardian3_integrity"

    def cycle(self):
        c = self.conn
        if self.due("hash", 30):
            self.check_hashes()
        if self.due("claims", 60):
            self.check_claims()
        if self.due("holdout", 120):
            self.check_holdout()
        if self.due("counters", 120):
            self.check_counters()
        if self.due("repro", 300):
            self.check_reproducibility()
        if self.due("causal", 300):
            self.check_causality()

    def check_hashes(self):
        c = self.conn
        cur = trusted_code_hash()
        row = c.execute("SELECT value FROM meta WHERE key='trusted_code_hash_baseline'").fetchone()
        if row is None:
            with db.tx(c):
                c.execute("INSERT OR IGNORE INTO meta VALUES('trusted_code_hash_baseline',?)", (cur,))
            return
        ok = row[0] == cur
        self.note("trusted_code_hash", ok, "" if ok else "engine/validation/qualification code changed outside change control")
        if not ok:
            self.raise_incident("integrity", "critical", "trusted code changed without controlled approval",
                                "edit to engine/validation/qualify/config/data/indicators", "campaign paused; qualification claims frozen; run `nexus approve-code-change` after tests",
                                fp="code_changed")
            db.set_state(c, "desired", "paused")
        else:
            self.clear_incident("code_changed")

    def check_claims(self):
        c = self.conn
        for cand in c.execute("SELECT strategy_id,status,integrity_verified FROM candidates").fetchall():
            sid = cand["strategy_id"]
            a = qualify.assess(c, sid, integrity_verified=True)
            claimed_q = cand["status"] == "qualified"
            if a["status"] != "qualified" and claimed_q:
                with db.tx(c):
                    c.execute("UPDATE candidates SET status=?, stage=?, integrity_verified=0, blockers_json=? WHERE strategy_id=?",
                              (a["status"], a["stage"], json.dumps(a["blockers"]), sid))
                self.raise_incident("integrity", "critical", f"qualified claim for {sid} not supported by evidence: revoked", "inconsistent evidence",
                                    "status recomputed from stored validations", fp=f"revoke:{sid}")
                self.note("qualified_claim", False, sid, "revoked")
            if a["status"] == "qualified" and not cand["integrity_verified"]:
                ok, why = self.verify_candidate(sid)
                with db.tx(c):
                    c.execute("UPDATE candidates SET integrity_verified=?, integrity_checked_at=? WHERE strategy_id=?", (int(ok), time.time(), sid))
                    if ok:
                        a2 = qualify.assess(c, sid)
                        c.execute("UPDATE candidates SET status=?, stage=?, blockers_json=? WHERE strategy_id=?",
                                  (a2["status"], a2["stage"], json.dumps(a2["blockers"]), sid))
                        c.execute("UPDATE experiments SET status='qualified' WHERE strategy_id=?", (sid,))
                self.note("verify_candidate", ok, f"{sid}: {why}", "integrity_verified set" if ok else "withheld")

    def verify_candidate(self, sid):
        """Re-run the development evaluation from the stored spec and compare to what was recorded; check holdout log."""
        c = self.conn
        row = c.execute("SELECT spec_json FROM strategies WHERE id=?", (sid,)).fetchone()
        ns = S.normalize(json.loads(row[0]))
        ex = c.execute("SELECT trades,net,expectancy_r FROM experiments WHERE strategy_id=? AND stage='S2' ORDER BY id LIMIT 1", (sid,)).fetchone()
        r = R.evaluate(c, ns)
        if ex is None or r.get("m2") is None:
            return False, "no recorded S2 experiment"
        if ex["trades"] != r["m2"]["trades"] or abs(ex["net"] - r["m2"]["net"]) > 1e-6:
            return False, f"S2 not reproducible: stored net {ex['net']} vs rerun {r['m2']['net']}"
        ho = c.execute("SELECT COUNT(*) FROM holdout_access WHERE strategy_id=? AND passed>=0", (sid,)).fetchone()[0]
        if ho != 1:
            return False, f"holdout looks={ho} (must be exactly 1)"
        if not self._causal(ns):
            return False, "causality probe failed"
        return True, "S2 reproduced exactly, single holdout look, causality probe passed"

    def check_holdout(self):
        c = self.conn
        bad = []
        for a in c.execute("SELECT * FROM holdout_access WHERE passed>=0").fetchall():
            ok = {r["stage"] for r in c.execute("SELECT stage FROM validations WHERE strategy_id=? AND passed=1 AND created_at<=?",
                                               (a["strategy_id"], a["ts"]))}
            if not {"walkforward", "robustness"} <= ok:
                bad.append(a["strategy_id"])
        n = c.execute("SELECT COUNT(*) FROM holdout_access WHERE passed>=0").fetchone()[0]
        self.note("holdout_audit", not bad and n <= config.THRESHOLDS["ho_max_evaluations_total"], f"looks={n} violations={bad}")
        if bad:
            self.raise_incident("integrity", "critical", f"holdout accessed before prerequisite gates: {bad}", "protocol violation",
                                "affected candidates cannot be qualified", fp="holdout_violation")

    def check_counters(self):
        c = self.conn
        k = memory.counters(c)
        indep = {
            "closed_trades": c.execute("SELECT COALESCE(SUM(trades),0) FROM backtest_runs WHERE 1=1").fetchone()[0],
            "experiments_completed": sum(r[0] for r in c.execute("SELECT COUNT(*) FROM experiments GROUP BY status")),
            "unique_strategies": c.execute("SELECT COUNT(DISTINCT id) FROM strategies").fetchone()[0],
        }
        bad = {m: (k[m], v) for m, v in indep.items() if k[m] != v}
        # every experiment must reference a strategy; no orphan runs
        orphans = c.execute("SELECT COUNT(*) FROM experiments e LEFT JOIN strategies s ON s.id=e.strategy_id WHERE s.id IS NULL").fetchone()[0]
        self.note("counter_reconciliation", not bad and orphans == 0, f"mismatch={bad} orphans={orphans}")
        if bad or orphans:
            self.raise_incident("integrity", "error", f"counter mismatch {bad} orphans={orphans}", "write path bug", "", fp="counter_mismatch")

    def check_reproducibility(self):
        c = self.conn
        row = c.execute("SELECT strategy_id,trades,net FROM experiments WHERE stage IN ('S1','S2') AND status IN ('rejected','promising') "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
        if not row:
            return
        spec = json.loads(c.execute("SELECT spec_json FROM strategies WHERE id=?", (row["strategy_id"],)).fetchone()[0])
        r = R.evaluate(c, spec)
        m = r.get("metrics") or {}
        ok = m.get("trades") == row["trades"] and abs(m.get("net", 0) - row["net"]) < 1e-6
        self.note("reproducibility", ok, f"{row['strategy_id']} stored trades={row['trades']} net={row['net']:.6f} rerun trades={m.get('trades')} net={m.get('net', 0):.6f}")
        if not ok:
            self.raise_incident("integrity", "critical", f"experiment {row['strategy_id']} not reproducible", "non-determinism or data/engine change",
                                "", fp=f"repro:{row['strategy_id']}")

    def _causal(self, ns, cut_frac=None):
        """Look-ahead probe: signals computed on a truncated prefix must equal the full-data signals at all earlier bars."""
        m, split = R.get_market(self.conn, ns)
        n = split["dev_end"]
        rng = random.Random(len(ns["family"]) + n)
        cut = int(n * rng.uniform(0.5, 0.95))
        a = S.signals_for(ns, m.prefix(n))
        b = S.signals_for(ns, m.prefix(cut))
        lim = cut - 1
        same_side = np.array_equal(a.side[:lim], b.side[:lim])
        sa, sb = a.stop_dist[:lim], b.stop_dist[:lim]
        same_stop = np.allclose(sa, sb, equal_nan=True, rtol=1e-9, atol=1e-9)
        return same_side and same_stop

    def check_causality(self):
        rng = random.Random(time.time())
        fam = rng.choice([f for f in S.F if f not in ("hybrid", "inverse")])
        for _ in range(30):
            sp = S.make_spec(fam, rng.choice(["ETHUSDT", "BTCUSDT"]), rng.choice(["1h", "2h", "4h"]),
                             {k: rng.choice(v) for k, v in S.F[fam]["grid"].items()})
            try:
                ns = S.normalize(sp)
                break
            except ValueError:
                continue
        else:
            return
        ok = self._causal(ns)
        self.note("causality_probe", ok, f"{fam} {ns['symbol']} {ns['timeframe']}")
        if not ok:
            self.raise_incident("integrity", "critical", f"look-ahead detected in family {fam}", "signal uses future bars",
                                "family weight floored; investigate", fp=f"lookahead:{fam}")
            with db.tx(self.conn):
                memory.set_weight(self.conn, fam, 0.05, "guardian3: look-ahead probe failed")


GUARDIANS = {1: Reliability, 2: Stagnation, 3: Integrity}
