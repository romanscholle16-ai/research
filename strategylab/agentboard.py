"""Agent health + research milestones, computed from the shared research memory (the DB). Pure read; used by status export and the tracker."""
from __future__ import annotations
import json, time


def _ago(t): return None if not t else round(time.time() - float(t), 0)


def agents(db):
    now = time.time(); q = db.q; one = lambda sql, a=(): (q(sql, a) or [{}])[0]
    ds = db.get("director_state") or {}; dl = db.get("director_last") or {}
    out = []
    out.append(dict(agent="A Research Director", state=ds.get("mode", "?"), last_s=_ago(dl.get("ts")), detail=(dl.get("reason") or "")[:160],
                    metric=f"batches={one('SELECT COUNT(*) n FROM director_batches')['n']}, exhausted arms={len(ds.get('exhausted') or {})}"))
    gp = one("SELECT COUNT(DISTINCT spec_hash) n FROM strategies WHERE family='gp_rule'")["n"]
    out.append(dict(agent="B Strategy Inventor", state="active", last_s=_ago(one("SELECT MAX(created) t FROM strategies")["t"]), detail="GP grammar + 16 classic families + positioning atoms; surrogate-ranked", metric=f"distinct GP rules={gp}"))
    fd = one("SELECT COUNT(*) n, MAX(ts) t FROM failure_diagnoses")
    out.append(dict(agent="C Failure Forensics", state="active" if fd["n"] else "idle", last_s=_ago(fd["t"]), detail="counterfactual diagnosis (cost/exit/side/session/regime)", metric=f"diagnoses={fd['n']}"))
    rr = one("SELECT COUNT(*) n, MAX(ts) t FROM refine_runs")
    arch = one("SELECT COUNT(*) n FROM refine_runs WHERE final_json LIKE '%archived%'")["n"]
    out.append(dict(agent="D Repair & Evolution", state="active" if rr["n"] else "idle", last_s=_ago(rr["t"]), detail="ratcheted dev-only edits chosen by diagnosis hints", metric=f"runs={rr['n']}, archived(no edge)={arch}"))
    da = one("SELECT COUNT(*) n, MAX(ts) t FROM data_audit"); qu = db.get("quarantine") or {}
    out.append(dict(agent="E Data Integrity", state="alert" if qu else ("ok" if da["n"] else "idle"), last_s=_ago(da["t"]), detail=f"quarantined: {list(qu)[:6]}" if qu else "all audited datasets pass", metric=f"audits={da['n']}"))
    ex = one("SELECT COUNT(*) n FROM experiments WHERE started>?", (now - 3600,))["n"]
    out.append(dict(agent="F Backtest Engine", state="active" if ex else "idle", last_s=_ago(one("SELECT MAX(finished) t FROM experiments")["t"]), detail="vectorised signals, memoised features, passive-limit fills, gap-safe", metric=f"experiments/h={ex}"))
    out.append(dict(agent="G Risk & Leverage", state="active", last_s=None, detail="liquidation-aware $300 sim; 250x/500x scenarios, 5-500x sweep (scripts/leverage_sweep.py)", metric=f"preset={db.get('exchange_preset') or 'see settings'}"))
    fin = one("SELECT COUNT(*) n, MAX(finished) t FROM experiments WHERE stage IN ('final','recheck_placebo','portfolio_final')")
    out.append(dict(agent="H Red-Team Validation", state="active", last_s=_ago(fin["t"]), detail="OOS, walk-forward, placebo, look-ahead, cost x1.5/x2, down-regime, locked one-shot test, Sidak", metric=f"final-stage runs={fin['n']}"))
    out.append(dict(agent="I Supervisor", state=db.get("control") or "?", last_s=_ago(db.get("service_heartbeat")), detail="watchdog, per-worker restart/backoff, RSS limit, stalled-job reaper", metric=f"restarts_24h={db.get('restarts_24h', '?')}"))
    return out


def milestones(db):
    q = db.q; one = lambda sql, a=(): (q(sql, a) or [{}])[0]
    tot = one("SELECT COUNT(*) n FROM experiments WHERE synthetic=0")["n"]
    by = {(r["stage"], r["status"]): r["n"] for r in q("SELECT stage,status,COUNT(*) n FROM experiments WHERE synthetic=0 GROUP BY 1,2")}
    pre = one("SELECT COUNT(*) n FROM precheck_rejections")["n"]
    return dict(total_experiments=tot, distinct_hypotheses=one("SELECT COUNT(*) n FROM hyp_registry")["n"], duplicates_avoided=pre,
                early_rejections=by.get(("eligibility", "fail"), 0), passed_frequency_cost_screen=by.get(("eligibility", "pass"), 0),
                positive_screen=by.get(("screen", "pass"), 0), validated=by.get(("validate", "pass"), 0), final_pass=by.get(("final", "pass"), 0),
                qualified=one("SELECT COUNT(*) n FROM strategy_status WHERE status='BACKTEST_QUALIFIED'")["n"],
                paper_validated=one("SELECT COUNT(*) n FROM strategy_status WHERE status='PAPER_VALIDATED'")["n"],
                diagnoses=one("SELECT COUNT(*) n FROM failure_diagnoses")["n"],
                causes=[(r["primary_cause"], r["n"]) for r in q("SELECT primary_cause, COUNT(*) n FROM failure_diagnoses GROUP BY 1 ORDER BY n DESC LIMIT 8")],
                validated_per_1k_experiments=round(1000 * by.get(("validate", "pass"), 0) / max(tot, 1), 2))
