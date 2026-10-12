"""Qualification logic. A pure function of STORED evidence - Guardian 3 re-runs it independently, so a research
agent's assertion can never create a 'qualified' status."""
import json
from . import config, ENGINE_VERSION

ORDER = ["exploratory", "development", "walkforward", "robustness", "holdout", "paper_forward", "qualified"]
STAGE_KEYS = ["walkforward", "robustness", "holdout", "paper_forward"]


def evidence(conn, sid):
    ev = {}
    for r in conn.execute("SELECT * FROM validations WHERE strategy_id=? ORDER BY id", (sid,)):
        ev[r["stage"]] = r          # latest per stage
    return ev


def assess(conn, sid, integrity_verified=None):
    """Return {stage, status, gates{stage: 'passed'|'failed'|'pending'|'stale'}, blockers[]}."""
    cand = conn.execute("SELECT * FROM candidates WHERE strategy_id=?", (sid,)).fetchone()
    ev = evidence(conn, sid)
    th = config.trusted_hash()
    gates, blockers = {}, []
    prev_ok = True
    for st in STAGE_KEYS:
        r = ev.get(st)
        if r is None:
            gates[st] = "pending"
            blockers.append(f"{st} not yet evaluated")
            prev_ok = False
        elif r["thresholds_hash"] != th or r["engine_version"] != ENGINE_VERSION:
            gates[st] = "stale"
            blockers.append(f"{st} evidence is stale (thresholds/engine changed) - re-validate")
            prev_ok = False
        elif r["passed"] == 1 and prev_ok:
            gates[st] = "passed"
        elif r["passed"] == 1:
            gates[st] = "failed"
            blockers.append(f"{st} passed but an earlier gate is not passed (inconsistent evidence)")
        elif r["passed"] == 0:
            gates[st] = "failed"
            blockers.append(f"{st} failed: " + "; ".join(json.loads(r["metrics_json"]).get("failures", [])[:3]))
            prev_ok = False
        else:                                 # passed == -1 : in progress (paper-forward accumulating)
            gates[st] = "in_progress"
            blockers.append(f"{st} in progress: " + json.loads(r["metrics_json"]).get("progress", ""))
            prev_ok = False
    iv = bool(cand["integrity_verified"]) if integrity_verified is None and cand else bool(integrity_verified)
    passed = [s for s in STAGE_KEYS if gates[s] == "passed"]
    if len(passed) == 4 and iv:
        stage, status = "qualified", "qualified"
    elif len(passed) == 4:
        stage, status = "paper_forward", "awaiting_integrity_verification"
        blockers.append("independent integrity verification by Guardian 3 not yet recorded")
    else:
        stage = "development" if not passed else passed[-1]
        failed = [s for s in STAGE_KEYS if gates[s] == "failed"]
        status = "validation_failed" if failed else ("promising" if not passed else "in_validation")
    return {"stage": stage, "status": status, "gates": gates, "blockers": blockers, "integrity_verified": iv}
