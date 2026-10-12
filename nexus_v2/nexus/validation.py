"""Statistical validation stages 3-6. Fixed rules in, evidence rows out. No stage is decided by an agent."""
import json, math, time
import numpy as np

from . import config, data, db, engine, indicators as I, metrics, qualify, research as R, strategies as S, ENGINE_VERSION

T = config.THRESHOLDS


def _purge(ns):
    p = ns["params"]
    return int(p.get("max_hold", 96)) if ns["family"] not in ("hybrid", "inverse") else 96


def _store_trades(conn, sid, phase, res, pm):
    tr = res.trades
    conn.execute("DELETE FROM candidate_trades WHERE strategy_id=? AND phase=?", (sid, phase))
    for k in range(len(tr["net"])):
        e, x = int(tr["entry_i"][k]), int(tr["exit_i"][k])
        conn.execute("INSERT INTO candidate_trades(strategy_id,phase,entry_ts,exit_ts,side,entry_px,exit_px,qty,net,fees,funding,"
                     "slippage,r,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, phase, int(pm.t[e]), int(pm.t[x]), int(tr["side"][k]), float(tr["entry_px"][k]), float(tr["exit_px"][k]),
                      float(tr["qty"][k]), float(tr["net"][k]), float(tr["fees"][k]), float(tr["funding"][k]),
                      float(tr["slip"][k]), float(tr["r"][k]), str(tr["reason"][k])))


def _record(conn, sid, stage, passed, n_trades, mt, dsv, runs):
    now = time.time()
    conn.execute("INSERT INTO validations(strategy_id,stage,passed,n_trades,metrics_json,thresholds_hash,engine_version,dataset_version,"
                 "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (sid, stage, passed, n_trades, json.dumps(R.jd(mt)), config.trusted_hash(),
                                                        ENGINE_VERSION, dsv, now))
    for kind, n in runs:
        conn.execute("INSERT INTO backtest_runs(kind,strategy_id,experiment_id,trades,created_at) VALUES(?,?,?,?,?)",
                     (f"{stage}:{kind}", sid, None, n, now))


def _wf_candidates_tested(conn):
    """M for the multiple-testing bar: candidates already sent to walk-forward (+1), never below the pre-registered expectation,
    so the bar does not depend on how early in the campaign a candidate happens to arrive."""
    seen = conn.execute("SELECT COUNT(DISTINCT strategy_id) FROM validations WHERE stage='walkforward'").fetchone()[0] + 1
    return max(seen, T["wf_expected_candidates"])


# ------------------------------------------------------------------------------ stage 3 ---------
def walkforward(conn, sid, ns, m, split):
    ee, de = split["explore_end"], split["dev_end"]
    purge = _purge(ns)
    start = ee + purge                              # purge: no trade may overlap parameter-selection data
    F = T["wf_folds"]
    edges = np.linspace(start, de, F + 1).astype(int)
    folds, rs, runs = [], [], []
    res_all = []
    for f in range(F):
        i0, i1 = int(edges[f]), int(edges[f + 1])
        res, mt, pm = R.eval_region(ns, m, i0, i1)
        runs.append((f"fold{f}", mt["trades"]))
        folds.append({"i0": i0, "i1": i1, "trades": mt["trades"], "exp_r": mt["expectancy_r"], "net": mt["net"], "pf": mt["pf"]})
        rs.extend(res.trades["r"].tolist())
        res_all.append((res, pm))
    rs = np.array(rs)
    n = len(rs)
    mean = float(rs.mean()) if n else 0.0
    sd = float(rs.std(ddof=1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 and n > 1 else 0.0
    lo, hi = metrics.block_bootstrap_mean(rs, level=T["wf_ci_level"]) if n >= 5 else (float("nan"), float("nan"))
    M = _wf_candidates_tested(conn)
    need_t = max(T["wf_min_multiple_testing_t"], math.sqrt(2 * math.log(M)))
    pos = sum(1 for f in folds if f["trades"] >= 3 and f["exp_r"] > 0)
    fails = []
    if n < T["wf_min_trades"]: fails.append(f"OOS trades {n} < {T['wf_min_trades']}")
    if mean <= 0: fails.append(f"OOS net expectancy {mean:+.3f}R <= 0")
    if not (lo > 0): fails.append(f"bootstrap lower bound {lo:+.3f}R not > 0")
    if t < need_t: fails.append(f"t={t:.2f} < multiple-testing bar {need_t:.2f} (M={M})")
    if pos / F < T["wf_min_positive_fold_frac"]: fails.append(f"only {pos}/{F} folds positive")
    if not m.funding_known: fails.append("funding history missing (assumed funding): non-qualifying")
    if m.data_class != "verified": fails.append(f"data class {m.data_class}")
    passed = int(not fails)
    mt = {"trades": n, "exp_r": mean, "t": t, "ci_lo": lo, "ci_hi": hi, "need_t": need_t, "M": M, "folds": folds,
          "positive_folds": pos, "purge_bars": purge, "failures": fails, "region": [start, de]}
    return passed, mt, runs, res_all


# ------------------------------------------------------------------------ randomisation test ---------
def null_test(ns, m, i0, i1, k, seed=11):
    """Random entries (same rate, stops, targets, holds) vs the candidate on region [i0,i1). p = P(null mean R >= candidate mean R)."""
    res, mt, pm = R.eval_region(ns, m, 0 if False else i0, i1)
    if mt["trades"] < 10:
        return None
    sig = S.signals_for(ns, pm)
    rng = np.random.default_rng(seed)
    prob = mt["trades"] / max(1, i1 - i0)
    acct, costs = R.account_from(ns), R.costs_default()
    nulls = []
    for _ in range(k):
        hit = rng.random(i1) < prob
        d = rng.random(i1) < 0.5
        side = np.where(hit, np.where(d, 1, -1), 0).astype(np.int8)
        s2 = engine.Signals(side=side, stop_dist=sig.stop_dist, tp_r=sig.tp_r, max_hold=sig.max_hold)
        rr = engine.run_backtest(pm, s2, acct, costs, i0, i1)
        nulls.append(float(rr.trades["r"].mean()) if len(rr.trades["r"]) else -1.0)
    nulls = np.array(nulls)
    return {"p_value": float((1 + (nulls >= mt["expectancy_r"]).sum()) / (k + 1)), "cand_exp_r": mt["expectancy_r"], "null_mean": float(nulls.mean()),
            "null_p95": float(np.quantile(nulls, .95)), "k": k, "region": [i0, i1]}


# ------------------------------------------------------------------------------ stage 4 ---------
def robustness(conn, sid, ns, m, split):
    de = split["dev_end"]
    res, mt0, pm = R.eval_region(ns, m, 0, de)
    runs = [("dev_full", mt0["trades"])]
    r = res.trades["r"].astype(float)
    fails, out = [], {"dev": mt0}
    if mt0["trades"] < T["rb_min_total_trades"]:
        fails.append(f"dev trades {mt0['trades']} < {T['rb_min_total_trades']}")
    if mt0["liquidations"] > 0:
        fails.append(f"{mt0['liquidations']} liquidations in development data")
    # parameter sensitivity
    nb = S.neighbors(ns)[:10]
    posn = 0
    nbres = []
    for sp in nb:
        _, mn, _ = R.eval_region(sp, m, 0, de)
        runs.append(("neighbor", mn["trades"]))
        nbres.append(mn["expectancy_r"])
        posn += mn["expectancy_r"] > 0 and mn["trades"] >= 20
    frac = posn / len(nb) if nb else 0.0
    out["neighbors"] = {"n": len(nb), "positive_frac": frac, "exp_r": nbres}
    if len(nb) < T["rb_min_neighbors"]:
        fails.append(f"only {len(nb)} parameter neighbours testable (< {T['rb_min_neighbors']})")
    elif frac < T["rb_min_neighbor_positive_frac"]:
        fails.append(f"only {frac:.0%} of parameter neighbours profitable")
    # cost / funding stress
    c2 = engine.Costs.default().stressed(T["rb_cost_stress_multiplier"])
    _, ms, _ = R.eval_region(ns, m, 0, de, costs=c2)
    runs.append(("cost_stress", ms["trades"]))
    out["cost_stress"] = {"exp_r": ms["expectancy_r"], "pf": ms["pf"]}
    if ms["expectancy_r"] <= 0:
        fails.append(f"expectancy {ms['expectancy_r']:+.3f}R under {T['rb_cost_stress_multiplier']}x fees+slippage")
    cf = engine.Costs.default(); cf.funding_mult = T["rb_funding_stress_multiplier"]
    _, mf, _ = R.eval_region(ns, m, 0, de, costs=cf)
    runs.append(("funding_stress", mf["trades"]))
    out["funding_stress"] = {"exp_r": mf["expectancy_r"]}
    if mf["expectancy_r"] <= 0:
        fails.append("non-positive expectancy under 3x funding")
    # outlier dependence
    if len(r) > 10:
        trimmed = np.sort(r)[:-T["rb_outlier_trim"]]
        out["trimmed_exp_r"] = float(trimmed.mean())
        if trimmed.mean() <= 0:
            fails.append(f"expectancy depends on the {T['rb_outlier_trim']} best trades")
    # volatility regimes (tercile of trailing vol percentile at decision bar)
    vp = I.vol_pct(pm)
    ei = res.trades["entry_i"].astype(int) - 1
    reg = np.digitize(np.nan_to_num(vp[np.clip(ei, 0, len(vp) - 1)], nan=0.5), [1 / 3, 2 / 3]) if len(ei) else np.array([])
    rg = {}
    nonneg = 0
    for g in range(3):
        sel = r[reg == g] if len(r) else np.array([])
        rg[g] = {"n": int(len(sel)), "exp_r": float(sel.mean()) if len(sel) else None}
        if len(sel) >= 10 and sel.mean() >= 0:
            nonneg += 1
    out["regimes"] = rg
    if nonneg < T["rb_min_regimes_nonneg"]:
        fails.append(f"non-negative in only {nonneg}/3 volatility regimes")
    # Monte Carlo: drawdown distribution + risk of ruin (block bootstrap of trade R)
    yrs = max(1e-9, (pm.t[de - 1] - pm.t[0]) / (365.25 * 86_400_000))
    horizon = max(30, int(len(r) / yrs * 0.5))      # trades in ~6 months
    mc = metrics.monte_carlo(r, config.RISK["risk_per_trade"], config.CAPITAL["start_equity"], T["rb_mc_paths"], horizon,
                             T["ruin_equity_fraction"]) if len(r) >= 30 else {"p_ruin": 1.0, "dd_p95": 1.0, "dd_p50": 1.0}
    out["monte_carlo"] = {**mc, "horizon_trades": horizon, "assumptions": f"risk {config.RISK['risk_per_trade']:.1%}/trade compounding, "
                          f"ruin = equity <= {T['ruin_equity_fraction']:.0%} of start, block-bootstrapped dev trades"}
    if mc["dd_p95"] > T["max_drawdown"]:
        fails.append(f"95th pct MC drawdown {mc['dd_p95']:.1%} > {T['max_drawdown']:.0%}")
    if mc["p_ruin"] > T["max_risk_of_ruin"]:
        fails.append(f"risk of ruin {mc['p_ruin']:.1%} > {T['max_risk_of_ruin']:.0%}")
    # out-of-sample randomisation test on the purged walk-forward region (selection-free for this candidate)
    nt = null_test(ns, m, split["explore_end"] + _purge(ns), de, T["adv_null_runs"])
    out["randomisation_oos"] = nt
    runs.append(("randomisation_nulls", 0))
    if nt is None or nt["p_value"] > T["adv_min_p"]:
        fails.append("out-of-sample randomisation test: " + ("too few trades" if nt is None else f"p={nt['p_value']:.3f} > {T['adv_min_p']} (random entries with the same exits do as well)"))
    out["failures"] = fails
    return int(not fails), out, runs, (res, pm)


# ------------------------------------------------------------------------------ stage 5 ---------
def holdout(conn, sid, ns, m, split):
    i0, i1 = data.holdout_region(conn, m, split, sid, "single-look final validation", "validation.holdout_stage")
    res, mt, pm = R.eval_region(ns, m, i0, i1)
    fails = []
    if mt["trades"] < T["ho_min_trades"]: fails.append(f"holdout trades {mt['trades']} < {T['ho_min_trades']}")
    if mt["expectancy_r"] <= 0: fails.append(f"holdout net expectancy {mt['expectancy_r']:+.3f}R <= 0")
    if mt["pf"] < T["ho_min_pf"]: fails.append(f"holdout PF {mt['pf']:.2f} < {T['ho_min_pf']}")
    if mt["t_stat"] < T["ho_min_t"]: fails.append(f"holdout t={mt['t_stat']:.2f} < {T['ho_min_t']} (not distinguishable from zero)")
    if mt["max_dd"] > T["max_drawdown"]: fails.append(f"holdout max drawdown {mt['max_dd']:.1%} > {T['max_drawdown']:.0%}")
    mt["failures"] = fails
    conn.execute("UPDATE holdout_access SET passed=? WHERE strategy_id=? AND passed=0", (int(not fails), sid))
    return int(not fails), mt, [("holdout", mt["trades"])], (res, pm)


# ------------------------------------------------------------------------------ stage 6 ---------
def paper_forward(conn, sid, ns, m, split, enrolled_ts):
    """Forward = bars AFTER the fixed holdout end (never seen by research). Needs accumulating real data."""
    i0 = int(np.searchsorted(m.t, split["holdout_end_ts"]))
    n = len(m.t)
    if n - i0 < 2:
        mt = {"progress": "0 forward bars available yet", "forward_days": 0.0, "trades": 0, "failures": []}
        return -1, mt, [], None
    res, mt, pm = R.eval_region(ns, m, i0, n)
    days = (m.t[-1] + m.tf_ms - m.t[i0]) / 86_400_000
    mt["forward_days"] = days
    fails = []
    if days < T["pf_min_days"] or mt["trades"] < T["pf_min_trades"]:
        mt["progress"] = f"{days:.1f}/{T['pf_min_days']} days, {mt['trades']}/{T['pf_min_trades']} trades"
        mt["failures"] = []
        return -1, mt, [("forward", mt["trades"])], (res, pm)
    if mt["expectancy_r"] <= 0: fails.append(f"forward net expectancy {mt['expectancy_r']:+.3f}R <= 0")
    if mt["pf"] < T["pf_min_pf"]: fails.append(f"forward PF {mt['pf']:.2f} < {T['pf_min_pf']}")
    mt["failures"] = fails
    return int(not fails), mt, [("forward", mt["trades"])], (res, pm)


# ----------------------------------------------------------------------------- orchestrator ------
def update_candidate(conn, sid):
    a = qualify.assess(conn, sid)
    conn.execute("UPDATE candidates SET stage=?, status=?, blockers_json=?, updated_at=? WHERE strategy_id=?",
                 (a["stage"], a["status"], json.dumps(a["blockers"]), time.time(), sid))
    conn.execute("UPDATE experiments SET status=? WHERE strategy_id=? AND status='promising' AND ?='validation_failed'",
                 (a["status"], sid, a["status"]))
    return a


def run_validation(conn, sid, progress=lambda s: None):
    row = conn.execute("SELECT spec_json FROM strategies WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise KeyError(sid)
    ns = S.normalize(json.loads(row[0]))
    m, split = R.get_market(conn, ns)
    dsv = m.version
    # only evidence recorded under the CURRENT thresholds+engine counts; superseded evidence is stale and the stage is re-run
    # (the holdout cannot be re-run: single look - see data.holdout_region)
    done = {r["stage"]: r for r in conn.execute("SELECT stage,passed FROM validations WHERE strategy_id=? AND thresholds_hash=? AND engine_version=? ORDER BY id",
                                                (sid, config.trusted_hash(), ENGINE_VERSION))}
    notes = []
    # stage 3
    if "walkforward" not in done:
        progress("walkforward")
        passed, mt, runs, res_all = walkforward(conn, sid, ns, m, split)
        with db.tx(conn):
            _record(conn, sid, "walkforward", passed, mt["trades"], mt, dsv, runs)
            for f, (res, pm) in enumerate(res_all):
                _store_trades(conn, sid, f"wf{f}", res, pm)
            update_candidate(conn, sid)
        done["walkforward"] = {"passed": passed}
        notes.append(f"walkforward={'PASS' if passed else 'FAIL'}")
    if not done["walkforward"]["passed"]:
        return notes
    if "robustness" not in done:
        progress("robustness")
        passed, mt, runs, (res, pm) = robustness(conn, sid, ns, m, split)
        with db.tx(conn):
            _record(conn, sid, "robustness", passed, mt["dev"]["trades"], mt, dsv, runs)
            _store_trades(conn, sid, "dev", res, pm)
            update_candidate(conn, sid)
        done["robustness"] = {"passed": passed}
        notes.append(f"robustness={'PASS' if passed else 'FAIL'}")
    if not done["robustness"]["passed"]:
        return notes
    if "holdout" not in done:
        progress("holdout")
        try:
            with db.tx(conn):      # access check + log commit atomically with the evaluation
                passed, mt, runs, (res, pm) = holdout(conn, sid, ns, m, split)
                _record(conn, sid, "holdout", passed, mt["trades"], mt, dsv, runs)
                _store_trades(conn, sid, "holdout", res, pm)
                update_candidate(conn, sid)
            notes.append(f"holdout={'PASS' if passed else 'FAIL'}")
        except data.HoldoutDenied as e:
            notes.append(f"holdout denied: {e}")
            conn.execute("UPDATE candidates SET blockers_json=? WHERE strategy_id=?", (json.dumps([f"holdout denied: {e}"]), sid))
        return notes
    return notes


def refresh_paper_forward(conn, sid):
    row = conn.execute("SELECT spec_json FROM strategies WHERE id=?", (sid,)).fetchone()
    ns = S.normalize(json.loads(row[0]))
    m, split = R.get_market(conn, ns)
    ev = qualify.evidence(conn, sid)
    if "holdout" not in ev or ev["holdout"]["passed"] != 1:
        return None
    passed, mt, runs, rp = paper_forward(conn, sid, ns, m, split, None)
    with db.tx(conn):
        conn.execute("DELETE FROM validations WHERE strategy_id=? AND stage='paper_forward' AND passed=-1", (sid,))
        _record(conn, sid, "paper_forward", passed, mt.get("trades", 0), mt, m.version, runs)
        if rp is not None and passed != -1:
            _store_trades(conn, sid, "forward", rp[0], rp[1])
        update_candidate(conn, sid)
    return passed


# ----------------------------------------------------------------- adversarial randomisation -----
def adversarial_test(conn, sid, k=100, seed=11):
    """Agent 10's challenge: do random entries with the SAME stop/target/hold rules do as well as this candidate on the
    exploration region? p = P(null expectancy >= candidate expectancy). Informational + blocks 'qualified' claims upstream."""
    row = conn.execute("SELECT spec_json FROM strategies WHERE id=?", (sid,)).fetchone()
    ns = S.normalize(json.loads(row[0]))
    m, split = R.get_market(conn, ns)
    ee = split["explore_end"]
    res, mt, pm = R.eval_region(ns, m, 0, ee)
    if mt["trades"] < 10:
        return None
    sig = S.signals_for(ns, pm)
    rng = np.random.default_rng(seed)
    prob = mt["trades"] / max(1, ee)
    acct = R.account_from(ns)
    costs = R.costs_default()
    nulls = []
    for _ in range(k):
        hit = rng.random(ee) < prob
        d = rng.random(ee) < 0.5
        side = np.where(hit, np.where(d, 1, -1), 0).astype(np.int8)
        s2 = engine.Signals(side=side, stop_dist=sig.stop_dist, tp_r=sig.tp_r, max_hold=sig.max_hold)
        rr = engine.run_backtest(pm, s2, acct, costs, 0, ee)
        nulls.append(float(rr.trades["r"].mean()) if len(rr.trades["r"]) else -1.0)
    nulls = np.array(nulls)
    p = float((1 + (nulls >= mt["expectancy_r"]).sum()) / (k + 1))
    out = {"p_value": p, "cand_exp_r": mt["expectancy_r"], "null_mean": float(nulls.mean()), "null_p95": float(np.quantile(nulls, .95)),
           "k": k, "IN_SAMPLE_selection_biased": True,
           "verdict": "in-sample only (candidate was SELECTED on this region, so this is not evidence); the gating test runs out-of-sample in robustness",
           "failures": []}
    with db.tx(conn):
        conn.execute("INSERT INTO validations(strategy_id,stage,passed,n_trades,metrics_json,thresholds_hash,engine_version,dataset_version,"
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (sid, "adversarial", -1, mt["trades"], json.dumps(R.jd(out)),
                                                            config.trusted_hash(), ENGINE_VERSION, m.version, time.time()))
        conn.execute("INSERT INTO backtest_runs(kind,strategy_id,experiment_id,trades,created_at) VALUES('adversarial_null',?,NULL,?,?)",
                     (sid, int(0), time.time()))
    return out
