"""Persistent research memory: queries over experiments/hypotheses used by agents, guardians and the dashboard."""
import json, time
from . import config, ENGINE_VERSION
from . import strategies as S


def family_stats(conn):
    rows = conn.execute("""SELECT family,
        COUNT(*) n,
        SUM(status IN ('rejected','inconclusive','promising','validation_failed','qualified')) valid,
        SUM(status='inconclusive') inconc,
        SUM(status IN ('promising','validation_failed','qualified')) prom,
        SUM(stage='S2') reached_s2,
        MAX(CASE WHEN stage='S2' THEN expectancy_r END) best_exp,
        AVG(CASE WHEN trades>0 THEN expectancy_r END) avg_exp
        FROM experiments GROUP BY family""").fetchall()
    out = {}
    for r in rows:
        out[r["family"]] = dict(n=r["n"], valid=r["valid"] or 0, inconclusive=r["inconc"] or 0, promising=r["prom"] or 0,
                                reached_s2=r["reached_s2"] or 0, best_exp=r["best_exp"], avg_exp=r["avg_exp"])
    return out


def weights(conn):
    return {r["family"]: r["weight"] for r in conn.execute("SELECT family,weight FROM family_weights")}


def set_weight(conn, family, weight, reason):
    conn.execute("INSERT INTO family_weights(family,weight,reason,updated_at) VALUES(?,?,?,?) ON CONFLICT(family) DO UPDATE SET "
                 "weight=excluded.weight, reason=excluded.reason, updated_at=excluded.updated_at",
                 (family, max(0.05, min(3.0, weight)), reason, time.time()))


def near_misses(conn, family=None, limit=20, min_trades=40):
    q = ("SELECT e.strategy_id, e.family, e.agent, e.expectancy_r, e.t_stat, e.trades, e.status, s.spec_json FROM experiments e "
         "JOIN strategies s ON s.id=e.strategy_id WHERE e.stage='S2' AND e.trades>=? AND e.expectancy_r>0")
    args = [min_trades]
    if family:
        q += " AND e.family=?"; args.append(family)
    q += " ORDER BY e.t_stat DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


def counters(conn):
    """Authoritative counters, always derived from rows (never from in-memory increments)."""
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    return {
        "closed_trades": q("SELECT COALESCE(SUM(trades),0) FROM backtest_runs"),
        "backtests": q("SELECT COUNT(*) FROM backtest_runs"),
        "unique_strategies": q("SELECT COUNT(*) FROM strategies"),
        "experiments_completed": q("SELECT COUNT(*) FROM experiments"),
        "hypotheses": q("SELECT COUNT(*) FROM hypotheses"),
        "hypotheses_rejected": q("SELECT COUNT(*) FROM hypotheses WHERE status='rejected'"),
        "promising": q("SELECT COUNT(*) FROM experiments WHERE status IN ('promising','validation_failed','qualified')"),
        "invalid": q("SELECT COUNT(*) FROM experiments WHERE status='invalid'"),
        "data_failures": q("SELECT COUNT(*) FROM experiments WHERE status='data_failure'"),
        "inconclusive": q("SELECT COUNT(*) FROM experiments WHERE status='inconclusive'"),
        "rejected": q("SELECT COUNT(*) FROM experiments WHERE status='rejected'"),
        "candidates": q("SELECT COUNT(*) FROM candidates"),
        "qualified": q("SELECT COUNT(*) FROM candidates WHERE status='qualified'"),
        "validation_runs": q("SELECT COUNT(*) FROM validations"),
        "validation_passes": q("SELECT COUNT(*) FROM validations WHERE passed=1"),
        "queue_pending": q("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
        "queue_running": q("SELECT COUNT(*) FROM jobs WHERE status='running'"),
        "quarantined": q("SELECT COUNT(*) FROM jobs WHERE status='quarantined'"),
        "duplicate_rejections": int(conn.execute("SELECT COALESCE(SUM(CAST(value AS INTEGER)),0) FROM runtime_state WHERE key='dup_rejected'").fetchone()[0]),
        "holdout_looks": q("SELECT COUNT(*) FROM holdout_access WHERE passed>=0"),
        "incidents_open": q("SELECT COUNT(*) FROM incidents WHERE resolved_at IS NULL"),
        "incidents_total": q("SELECT COUNT(*) FROM incidents"),
    }


def window_activity(conn, seconds):
    now = time.time()
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    return {"experiments": q("SELECT COUNT(*) FROM experiments WHERE completed_at>?", now - seconds),
            "informative": q("SELECT COUNT(*) FROM experiments WHERE completed_at>? AND status IN "
                             "('rejected','promising','validation_failed','qualified')", now - seconds),
            "promising": q("SELECT COUNT(*) FROM experiments WHERE completed_at>? AND status='promising'", now - seconds),
            "new_hypothesis_status": q("SELECT COUNT(*) FROM hypotheses WHERE updated_at>? AND status NOT IN ('open')", now - seconds),
            "validations": q("SELECT COUNT(*) FROM validations WHERE created_at>?", now - seconds),
            "invalid": q("SELECT COUNT(*) FROM experiments WHERE completed_at>? AND status='invalid'", now - seconds),
            "jobs_done": q("SELECT COUNT(*) FROM jobs WHERE finished_at>? AND status='done'", now - seconds)}
