"""Experiment evaluation (stages 1-2) and persistence. This is the shared, trusted path every agent uses."""
import json, time
import numpy as np

from . import config, data, db, engine, metrics, strategies as S, ENGINE_VERSION

_MK = {}


def jd(o):
    if isinstance(o, dict):
        return {str(k): jd(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jd(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if np.isfinite(f) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def get_market(conn, spec):
    sym, tf = spec["symbol"], spec["timeframe"]
    other = S.other_symbol(sym) if S.needs_other(spec) else None
    m = data.load_market(sym, tf, other)
    split = data.get_split(conn, sym, tf, m)
    return m, split


_EV = {}


def explore_version(conn, spec):
    """Hash of the data inside the fixed exploration region (+ aligned partner symbol). Appending new bars does not
    change it, so settled experiments are not re-run; corrected historical data would."""
    import hashlib
    ns = S.normalize(spec)
    m, split = get_market(conn, ns)
    key = (m.uid, split["explore_end"])
    v = _EV.get(key)
    if v is None:
        ee = split["explore_end"]
        h = hashlib.sha256()
        for a in (m.t[:ee], m.o[:ee], m.h[:ee], m.l[:ee], m.c[:ee], m.v[:ee], m.tbv[:ee]):
            h.update(np.ascontiguousarray(a).tobytes())
        if m.xc is not None:
            h.update(np.ascontiguousarray(m.xc[:ee]).tobytes())
        fe = m.fund_t <= m.t[ee - 1]
        h.update(np.ascontiguousarray(m.fund_r[fe]).tobytes())
        v = _EV[key] = h.hexdigest()[:12]
    return v


def costs_default(**o):
    return engine.Costs.default(**o)


def account_from(spec, **o):
    a = S.normalize(spec)["account"]
    return engine.Account(start_equity=config.CAPITAL["start_equity"], risk_pct=a["risk_pct"], leverage=a["leverage"],
                          daily_loss_halt=config.RISK["daily_loss_halt"], drawdown_halt=config.RISK["drawdown_halt"], **o)


def run_region(spec, m, i0, i1, costs=None, acct=None):
    """Compute signals on the PREFIX [0,i1) only (no future bars exist for them) and simulate [i0,i1)."""
    ns = S.normalize(spec)
    pm = m.prefix(i1)
    sig = S.signals_for(ns, pm)
    res = engine.run_backtest(pm, sig, acct or account_from(ns), costs or costs_default(), i0, i1)
    return res, pm, sig


def eval_region(spec, m, i0, i1, costs=None, acct=None):
    res, pm, sig = run_region(spec, m, i0, i1, costs, acct)
    return res, metrics.summarize(res), pm


def _lesson(st, mt, stage):
    if mt["invalid"]:
        return f"invalid configuration: {mt['invalid']}"
    if mt["trades"] == 0:
        return "no trades generated; signal too rare or parameters degenerate"
    g, f = mt["gross"], mt["fees"] + mt["slippage"]
    cost_share = f / abs(g) if abs(g) > 1e-9 else float("inf")
    base = f"{stage}: {mt['trades']} trades, net expectancy {mt['expectancy_r']:+.3f}R (t={mt['t_stat']:.2f}), PF {mt['pf']:.2f}, maxDD {mt['max_dd']:.1%}"
    if g > 0 and mt["net"] <= 0:
        base += f"; gross edge positive but costs consumed it (fees+slippage = {cost_share:.0%} of gross)"
    elif g <= 0:
        base += "; no gross edge before costs"
    return base


def evaluate(conn_or_none, spec, conn=None):
    """Pure evaluation (no DB writes). Returns dict with status, metrics for S1/S2, runs list."""
    conn = conn or conn_or_none
    ns = S.normalize(spec)
    out = {"spec": ns, "runs": [], "stage": "S1", "status": "rejected", "reason": ""}
    try:
        m, split = get_market(conn, ns)
    except Exception as e:  # data not available -> data_failure, not a research result
        return {**out, "status": "data_failure", "reason": f"market data unavailable: {e}"}
    man = conn.execute("SELECT manifest_json FROM datasets WHERE symbol=? ORDER BY id DESC LIMIT 1", (ns["symbol"],)).fetchone()
    if man and json.loads(man[0]).get("gap_frac", 0) > config.THRESHOLDS["max_gap_frac"]:
        return {**out, "status": "data_failure", "reason": "gap fraction above limit"}
    out["dataset_version"] = explore_version(conn, ns)
    T = config.THRESHOLDS
    ee = split["explore_end"]
    t0 = time.perf_counter()
    res1, m1, _ = eval_region(ns, m, 0, ee // 2)
    out["runs"].append(("S1", m1["trades"]))
    out["m1"] = m1
    out["lesson"] = _lesson(None, m1, "S1")
    if m1["invalid"]:
        return {**out, "status": "invalid", "reason": m1["invalid"], "metrics": m1}
    if m1["trades"] < T["s1_min_trades"]:
        return {**out, "status": "inconclusive", "reason": f"S1 trades {m1['trades']} < {T['s1_min_trades']}", "metrics": m1}
    if m1["expectancy_r"] <= 0:
        return {**out, "status": "rejected", "reason": "S1 non-positive net expectancy", "metrics": m1}
    res2, m2, pm = eval_region(ns, m, 0, ee)
    out["runs"].append(("S2", m2["trades"]))
    out.update(stage="S2", m2=m2, metrics=m2, lesson=_lesson(None, m2, "S2"))
    ok = (m2["trades"] >= T["s2_min_trades"] and m2["expectancy_r"] > 0 and m2["t_stat"] >= T["s2_min_t"] and m2["pf"] >= T["s2_min_pf"])
    if m2["trades"] < T["s2_min_trades"]:
        return {**out, "status": "inconclusive", "reason": f"S2 trades {m2['trades']} < {T['s2_min_trades']}"}
    if not ok:
        return {**out, "status": "rejected", "reason": "S2 thresholds not met"}
    if ns["family"] == "hybrid":
        ab = {}
        for key in ("a", "b"):
            comp = {**ns["params"][key], "symbol": ns["symbol"], "timeframe": ns["timeframe"]}
            _, mc, _ = eval_region(comp, m, 0, ee)
            out["runs"].append((f"ablation_{key}", mc["trades"]))
            ab[key] = mc["expectancy_r"]
        out["ablation"] = {"hybrid": m2["expectancy_r"], **ab}
        if m2["expectancy_r"] < max(ab.values()) + 0.03:
            return {**out, "status": "rejected", "reason": "ablation failed: hybrid does not beat its best component"}
    if ns["family"] in ("random_entry", "periodic_hold"):
        # null/baseline models calibrate the false-positive rate of the screen; they are never promoted to candidates
        out["status"] = "rejected"
        out["reason"] = "null model passed the S2 screen by chance (false-positive calibration); not eligible for promotion"
        out["lesson"] = "NULL MODEL PASSED S2: the screen has a non-zero false-positive rate; " + out.get("lesson", "")
        out["runtime_ms"] = (time.perf_counter() - t0) * 1000
        return out
    out["status"] = "promising"
    out["reason"] = "passed S1+S2 (exploratory evidence only; NOT qualified)"
    out["runtime_ms"] = (time.perf_counter() - t0) * 1000
    return out


def next_suggestion(r):
    st = r["status"]
    if st == "promising":
        return "run walk-forward validation; test parameter neighbours; adversarial randomisation test"
    if st == "inconclusive":
        return "too few trades: try shorter timeframe or looser trigger, or deprioritise this family variant"
    if st == "rejected":
        m = r.get("metrics") or {}
        if m.get("gross", 0) > 0 and m.get("net", 0) <= 0:
            return "gross edge exists but costs dominate: test lower-turnover variants (longer holds / higher timeframe)"
        return "no edge here: explore different parameter region or family; failure is evidence against this hypothesis"
    return "n/a"


def hypothesis_for(conn, agent, family, symbol, tf):
    info = S.F[family]["info"]
    r = conn.execute("SELECT id FROM hypotheses WHERE agent=? AND family=? AND symbol=? AND timeframe=?",
                     (agent, family, symbol, tf)).fetchone()
    if r:
        return r[0]
    cur = conn.execute("INSERT INTO hypotheses(agent,family,symbol,timeframe,text,rationale,falsifier,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (agent, family, symbol, tf, info["hypothesis"], info["rationale"],
                                                       info["falsifier"], "open", time.time(), time.time()))
    return cur.lastrowid


def update_hypothesis(conn, hid):
    """open -> supported / rejected based on accumulated valid experiments."""
    rows = conn.execute("SELECT status,COUNT(*) c FROM experiments WHERE hypothesis_id=? GROUP BY status", (hid,)).fetchall()
    cnt = {r[0]: r[1] for r in rows}
    n = sum(cnt.values())
    valid = n - cnt.get("invalid", 0) - cnt.get("data_failure", 0) - cnt.get("infra_failure", 0)
    prom = cnt.get("promising", 0) + cnt.get("validation_failed", 0) + cnt.get("qualified", 0)
    status = "open"
    if valid >= 40 and prom == 0:
        status = "rejected"
    elif prom >= 3:
        status = "supported_exploratory"
    elif valid >= 20:
        status = "inconclusive"
    conn.execute("UPDATE hypotheses SET n_experiments=?, status=?, updated_at=? WHERE id=?", (n, status, time.time(), hid))


def persist(conn, spec, result, agent, hypothesis_id=None, why="", job_id=None, seed=None, runtime_ms=None):
    """Write strategy+experiment+runs (+candidate) atomically. Idempotent: the UNIQUE key prevents double-counting
    when a job is replayed after a crash."""
    ns = result["spec"]
    sid = S.canonical_id(ns)
    m = result.get("metrics") or result.get("m1") or {}
    status = result["status"]
    now = time.time()
    with db.tx(conn):
        conn.execute("INSERT OR IGNORE INTO strategies VALUES(?,?,?,?,?,?,?,?)",
                     (sid, ns["family"], agent, ns["symbol"], ns["timeframe"], json.dumps(ns, sort_keys=True), None, now))
        cur = conn.execute(
            "INSERT OR IGNORE INTO experiments(strategy_id,hypothesis_id,agent,family,dataset_version,engine_version,phase,stage,"
            "status,failure_reason,seed,created_at,started_at,completed_at,runtime_ms,trades,gross,net,fees,funding,slippage,"
            "expectancy_r,exp_usd,t_stat,pf,win_rate,max_dd,metrics_json,lessons,next_suggestion,why_selected,job_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, hypothesis_id, agent, ns["family"], result.get("dataset_version", ""), ENGINE_VERSION, "explore",
             result["stage"], status, result["reason"][:500], seed, now, now, now, runtime_ms or result.get("runtime_ms"),
             m.get("trades", 0), m.get("gross", 0), m.get("net", 0), m.get("fees", 0), m.get("funding", 0), m.get("slippage", 0),
             m.get("expectancy_r", 0), m.get("exp_usd", 0), m.get("t_stat", 0), m.get("pf", 0), m.get("win_rate", 0),
             m.get("max_dd", 0), json.dumps(jd({"m1": result.get("m1"), "m2": result.get("m2"), "ablation": result.get("ablation")})),
             result.get("lesson", ""), next_suggestion(result), why[:500], job_id))
        if cur.rowcount == 0:
            return sid, False
        eid = cur.lastrowid
        for kind, n in result["runs"]:
            conn.execute("INSERT INTO backtest_runs(kind,strategy_id,experiment_id,trades,created_at) VALUES(?,?,?,?,?)",
                         (kind, sid, eid, n, now))
        if hypothesis_id:
            update_hypothesis(conn, hypothesis_id)
        if status == "promising":
            m2 = result["m2"]
            conn.execute("INSERT OR IGNORE INTO candidates(strategy_id,stage,status,promoted_at,updated_at,dev_exp_r,dev_t,dev_trades,"
                         "blockers_json,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (sid, "development", "promising", now, now, m2["expectancy_r"], m2["t_stat"], m2["trades"],
                          json.dumps(["walk-forward not run", "robustness not run", "holdout not run", "paper-forward not run"]),
                          json.dumps(jd({"dev": m2}))))
            conn.execute("INSERT OR IGNORE INTO jobs(type,payload,status,priority,created_at,dedupe_key,max_attempts) "
                         "VALUES('validate',?, 'pending', ?, ?, ?, 3)",
                         (json.dumps({"strategy_id": sid}), 3, now, f"validate:{sid}"))
    return sid, True
