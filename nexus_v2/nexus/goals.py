"""The 15 financial goals, evaluated from stored evidence only. Symbols: ok=✓ part=◐ fail=✕ unk=?"""
import json, time
import numpy as np

from . import config, db, exchange, memory, metrics, qualify
from .config import CAPITAL as C, THRESHOLDS as T

OK, PART, FAIL, UNK = "✓", "◐", "✕", "?"
TEXT = {OK: "Met", PART: "In progress", FAIL: "Failed", UNK: "Unverified / not yet tested"}


def usd(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def cand_stats(conn, sid):
    rows = conn.execute("SELECT phase,entry_ts,exit_ts,net,r FROM candidate_trades WHERE strategy_id=? ORDER BY exit_ts", (sid,)).fetchall()
    oos = [r for r in rows if r["phase"].startswith("wf") or r["phase"] in ("holdout", "forward")]
    dev = [r for r in rows if r["phase"] == "dev"]
    use, basis = (oos, "out-of-sample (walk-forward + holdout + forward)") if oos else (dev, "in-sample development data (NOT evidence)")
    out = {"basis": basis, "n_oos": len(oos), "n_dev": len(dev), "rows": use}
    if use:
        t = np.array([r["exit_ts"] for r in use]); net = np.array([r["net"] for r in use])
        span_ms = max(t.max() - t.min(), 30 * 86_400_000)
        months = span_ms / (30 * 86_400_000)
        mp = metrics.monthly_pnl(t, net)
        rng = np.random.default_rng(5)
        boots = [rng.choice(mp, len(mp)).mean() for _ in range(2000)] if len(mp) >= 3 else [net.sum() / months]
        out["monthly"] = {"mean": float(net.sum() / months), "p10": float(np.quantile(boots, .1)), "p90": float(np.quantile(boots, .9)),
                          "months": months, "positive_months": int((mp > 0).sum()), "n_months": int(len(mp))}
        # rolling 7-day coverage and 30-day activity
        day = 86_400_000
        grid = np.arange(t.min(), t.max() - 7 * day, day)
        if len(grid):
            ex = np.sort(t)
            cov = np.mean([(ex[np.searchsorted(ex, g):np.searchsorted(ex, g + 7 * day)].size > 0) for g in grid])
        else:
            cov = 1.0
        out["coverage7"] = float(cov)
        out["trades_per_30d"] = float(len(use) / (span_ms / (30 * day)))
    return out


def best_candidate(conn):
    r = conn.execute("""SELECT c.strategy_id FROM candidates c ORDER BY
        (c.status='qualified') DESC, (SELECT COUNT(*) FROM validations v WHERE v.strategy_id=c.strategy_id AND v.passed=1 AND
         v.stage IN ('walkforward','robustness','holdout','paper_forward')) DESC, c.dev_t DESC LIMIT 1""").fetchone()
    return r[0] if r else None


def compute(conn, sid=None):
    now = time.time()
    g = {}
    cand = a = None
    st = None
    if sid:
        a = qualify.assess(conn, sid)
        st = cand_stats(conn, sid)
        cand = conn.execute("SELECT * FROM candidates WHERE strategy_id=?", (sid,)).fetchone()
    gates = a["gates"] if a else {}
    ev = qualify.evidence(conn, sid) if sid else {}
    mets = lambda stage: json.loads(ev[stage]["metrics_json"]) if stage in ev else {}
    stage_name = a["stage"] if a else "none"

    def row(n, symbol, actual, evidence, explain, obstacle, progress=None):
        name, target = config.GOALS[n - 1][1], config.GOALS[n - 1][2]
        g[n] = {"n": n, "name": name, "target": target, "actual": actual, "symbol": symbol, "status": TEXT[symbol], "progress": progress,
                "evidence": evidence, "stage": stage_name, "explanation": explain, "obstacle": obstacle, "evaluated_at": now}

    # 1
    row(1, OK if C["start_equity"] == 300.0 else FAIL, f"${C['start_equity']:,.0f} simulated start equity (config v{config.CONFIG_VERSION})",
        "trusted config; engine tests use this value", "All simulations start from $300. This is a configured research assumption - not proof a funded real account exists.",
        "A real funded BTCC account is outside this research-only release.")
    # 2
    specs = conn.execute("SELECT status,COUNT(*) FROM exchange_specs WHERE exchange='BTCC' AND symbol='ETHUSDT' GROUP BY status").fetchall()
    cnt = {r[0]: r[1] for r in specs}
    ver, tot = cnt.get("verified", 0), sum(cnt.values())
    contract_verified = conn.execute("SELECT COUNT(*) FROM exchange_specs WHERE exchange='BTCC' AND symbol='ETHUSDT' AND status='verified' AND field!='price_data_venue'").fetchone()[0]
    row(2, OK if contract_verified >= 7 else UNK, f"{contract_verified} BTCC contract fields verified of 7; {cnt.get('assumed',0)} assumed, {cnt.get('unverified',0)} unverified",
        "exchange_specs registry", "BTCC contract specs, fees, margin tiers and funding could not be retrieved from authoritative documentation; conservative assumptions are used for research only. Price data is Binance USDT-M (a proxy).",
        "Verify BTCC ETHUSDT/BTCUSDT contract details from BTCC's official documentation and record them in the registry.")
    # 3/4
    for n, L in ((3, 250), (4, 500)):
        fe = exchange.leverage_feasibility(L)
        s_, v_, note = exchange.spec_status(conn, "ETHUSDT", f"max_leverage_{L}x")
        sym = UNK
        if s_ == "verified" and fe["valid_simulation"]:
            sym = OK
        elif s_ == "verified" and not fe["valid_simulation"]:
            sym = FAIL
        row(n, sym, f"availability: {s_}; simulation validity: {'valid' if fe['valid_simulation'] else 'INVALID'} (liquidation buffer {fe['liquidation_buffer_frac']*100:+.3f}% of price after assumed maintenance margin and fees)",
            "exchange_specs + engine leverage validity check", f"At {L}x the initial margin is {100/L:.2f}% of notional. {fe['note']}. Availability on BTCC for the specific contract/account is not verified.",
            f"Verify {L}x availability from official BTCC sources, and obtain real maintenance-margin tiers; under current assumptions the simulation is invalid at this leverage." if not fe["valid_simulation"] else "Verify availability from official BTCC sources.")
    # 5
    if st and "coverage7" in st:
        cv = st["coverage7"]
        validated = gates.get("walkforward") == "passed"
        sym = OK if cv >= T["freq_min_rolling7_coverage"] and validated else (FAIL if cv < 0.5 else PART)
        row(5, sym, f"{cv:.0%} of rolling 7-day windows contain >=1 trade; {st['trades_per_30d']:.1f} trades per 30 days ({st['basis']})", "candidate_trades",
            "Counts closed simulated trades in rolling windows over the evaluation period.",
            "" if sym == OK else ("Frequency too low for the weekly target." if cv < 0.5 else "Frequency adequate but candidate not yet validated."), cv)
    else:
        row(5, UNK, "Not yet measured", "no candidate with stored trades", "Needs a candidate with out-of-sample trades.", "No candidate has reached walk-forward evaluation.")
    # 6
    if sid and "walkforward" in ev:
        wf = mets("walkforward")
        if gates.get("walkforward") == "failed":
            row(6, FAIL, f"OOS net expectancy {wf.get('exp_r',0):+.3f}R over {wf.get('trades',0)} trades (t={wf.get('t',0):.2f}, 90% CI [{wf.get('ci_lo',0):+.3f}, {wf.get('ci_hi',0):+.3f}])",
                "walk-forward validation", "Failed walk-forward: " + "; ".join(wf.get("failures", [])[:2]), "; ".join(wf.get("failures", [])[:3]))
        else:
            ok = gates.get("robustness") == "passed"
            row(6, OK if ok else PART, f"OOS net expectancy {wf.get('exp_r',0):+.3f}R over {wf.get('trades',0)} trades (t={wf.get('t',0):.2f} vs bar {wf.get('need_t',0):.2f})",
                "walk-forward" + (" + robustness" if ok else ""), "Positive after modeled fees, funding and slippage on data not used for selection.",
                "" if ok else "Robustness analysis not yet passed.")
    elif cand:
        row(6, PART if cand["dev_exp_r"] and cand["dev_exp_r"] > 0 else UNK, f"in-sample development expectancy {cand['dev_exp_r']:+.3f}R ({cand['dev_trades']} trades) - exploratory only",
            "S2 screen", "In-sample result only; says nothing about out-of-sample expectancy.", "Walk-forward validation pending.")
    else:
        row(6, UNK, "Not yet measured", "none", "No candidate has been evaluated.", "No candidate has passed exploratory screening.")
    # 7-10 profit and withdrawals
    mo = st.get("monthly") if st else None
    holdout_ok = gates.get("holdout") == "passed"
    for n, name_t, target, wd in ((7, "min", C["min_monthly_net_profit"], False), (8, "stretch", C["stretch_monthly_net_profit"], False),
                                   (9, "wmin", C["min_monthly_withdrawal"], True), (10, "wstretch", C["stretch_monthly_withdrawal"], True)):
        if mo is None:
            row(n, UNK, "Not yet measured", "none", "Needs stored trades from a candidate.", "No candidate with trades.")
            continue
        val = mo["mean"] * (C["max_withdrawal_fraction"] if wd else 1.0)
        lo = mo["p10"] * (C["max_withdrawal_fraction"] if wd else 1.0)
        lab = "max withdrawal under 50% policy" if wd else "mean monthly net profit"
        prog = max(0.0, min(1.0, val / target))
        if val >= target and holdout_ok and lo > 0:
            sym = OK
        elif val >= target:
            sym = PART
        else:
            sym = FAIL
        row(n, sym, f"{usd(val)} {lab} (10th-90th pct of monthly mean: {usd(lo)} to {usd(mo['p90']*(0.5 if wd else 1))}); basis: {st['basis']}",
            "candidate_trades (net of modeled costs)",
            f"Required: ${target:,.0f}" + (f" withdrawal needs >= ${target/C['max_withdrawal_fraction']:,.0f} net profit/month at the 50% cap" if wd else "") +
            f"; on a $300 account that is {target/(C['max_withdrawal_fraction'] if wd else 1)/C['start_equity']:.0%} per month.",
            "" if sym == OK else (f"Estimated {usd(val)} is {prog:.1%} of target" + ("" if holdout_ok else "; also not holdout-validated")), prog)
    # 11
    if mo and mo["positive_months"] > 0:
        row(11, OK if holdout_ok else PART, f"Policy cap {C['max_withdrawal_fraction']:.0%} of positive monthly net profit; retention >=50% in all {mo['positive_months']} positive months (by construction of the simulator policy)",
            "finsim policy test", "Withdrawals never exceed half of a positive month's trading profit; deposits are excluded from profit.",
            "" if holdout_ok else "Candidate not yet holdout-validated.")
    else:
        row(11, UNK, "Not yet measured (no positive-profit months in evidence)", "none", "Policy is implemented in the financial simulator; it can only be evidenced against positive months.", "No profitable candidate months.")
    # 12
    ps = float(conn.execute("SELECT value FROM meta WHERE key='project_start'").fetchone()[0])
    day = (now - ps) / 86400
    horizon = C["project_horizon_months"] * 30.4
    achieved = sid and g[7]["symbol"] == OK and g[9]["symbol"] == OK and gates.get("holdout") == "passed"
    if achieved:
        row(12, OK, f"Day {day:.1f} of ~{horizon:.0f}: min profit & withdrawal evidenced", "goals 7 and 9", "Achieved inside the window.", "")
    elif day > horizon:
        row(12, FAIL, f"Window ended (day {day:.0f} > {horizon:.0f})", "project start date", "Deadline passed without validated evidence for goals 7 and 9.", "Objective not met within six months.")
    else:
        row(12, PART, f"Day {day:.1f} of ~{horizon:.0f}; no validated candidate evidences goals 7/9" if not achieved else "", "project start date + goals 7/9",
            "Tracking evidence toward the six-month objective; time elapsed is not progress toward the profit goal.", "A holdout-validated candidate with >= $2,000/month net estimate is required.")
    # 13
    rb = mets("robustness")
    if gates.get("robustness") == "passed":
        mc = rb.get("monte_carlo", {})
        row(13, OK, f"95th pct MC drawdown {mc.get('dd_p95',0):.1%} (limit {T['max_drawdown']:.0%}); risk of ruin {mc.get('p_ruin',0):.1%} (limit {T['max_risk_of_ruin']:.0%})",
            "robustness stage", "Assumptions: " + mc.get("assumptions", ""), "")
    elif gates.get("robustness") == "failed":
        row(13, FAIL, "; ".join(rb.get("failures", [])[:2]), "robustness stage", "Candidate breached risk limits or robustness checks.", "; ".join(rb.get("failures", [])[:3]))
    else:
        row(13, PART, f"Limits defined and enforced by engine (risk {config.RISK['risk_per_trade']:.0%}/trade, daily loss halt {config.RISK['daily_loss_halt']:.0%}, drawdown halt {config.RISK['drawdown_halt']:.0%}); no candidate yet shown to satisfy them",
            "engine tests", "Definition and enforcement exist; satisfaction must be shown per candidate by robustness analysis.", "Candidate must pass walk-forward then robustness.")
    # 14
    if gates.get("walkforward") == "failed" or gates.get("robustness") == "failed":
        row(14, FAIL, "failed: " + "; ".join((mets("walkforward") if gates.get("walkforward") == "failed" else rb).get("failures", [])[:2]), "validation stages", "Failed the independent validation protocol.", "Candidate rejected at validation.")
    elif gates.get("walkforward") == "passed" and gates.get("robustness") == "passed" and a["integrity_verified"]:
        row(14, OK, "walk-forward + robustness passed; Guardian 3 re-verified evidence", "validations + guardian3", "Independently reproduced and audited.", "")
    elif gates.get("walkforward") == "passed":
        row(14, PART, "walk-forward passed; robustness/integrity verification pending", "validations", "In progress.", "Robustness and independent integrity verification.")
    else:
        row(14, UNK, "Not yet measured", "none", "No candidate has completed walk-forward.", "No candidate has completed validation.")
    # 15
    ho, pf = mets("holdout"), mets("paper_forward")
    if gates.get("holdout") == "failed":
        row(15, FAIL, "holdout failed: " + "; ".join(ho.get("failures", [])[:2]), "protected holdout (single look)", "The holdout is spent for this candidate.", "Candidate rejected.")
    elif gates.get("paper_forward") == "failed":
        row(15, FAIL, "paper-forward failed: " + "; ".join(pf.get("failures", [])[:2]), "paper-forward", "", "Candidate rejected.")
    elif gates.get("paper_forward") == "passed" and a["integrity_verified"]:
        row(15, OK, "holdout and paper-forward passed; integrity verified", "validations", "All final gates passed.", "")
    elif gates.get("holdout") == "passed":
        row(15, PART, "holdout passed; paper-forward: " + pf.get("progress", "not started"), "holdout + paper-forward", "Forward qualification accumulates real elapsed time.", f"Needs {T['pf_min_days']} days / {T['pf_min_trades']} trades forward.")
    else:
        row(15, UNK, "Not yet measured", "none", "Holdout is only opened for candidates that passed walk-forward and robustness.", "Prerequisite stages not passed.")
    return [g[i] for i in range(1, 16)]


def leaderboards(conn, limit=15):
    A = []
    for r in conn.execute("SELECT c.strategy_id, c.stage, c.status, c.dev_exp_r, c.dev_t, c.dev_trades, s.family, s.symbol, s.timeframe, s.agent FROM candidates c "
                          "JOIN strategies s ON s.id=c.strategy_id ORDER BY c.dev_t DESC LIMIT ?", (limit,)):
        A.append({**dict(r), "label": "exploratory - UNQUALIFIED"})
    near = conn.execute("SELECT e.strategy_id, e.family, e.expectancy_r dev_exp_r, e.t_stat dev_t, e.trades dev_trades, s.symbol, s.timeframe, s.agent FROM experiments e "
                        "JOIN strategies s ON s.id=e.strategy_id WHERE e.stage='S2' AND e.status='rejected' AND e.expectancy_r>0 AND e.trades>=40 "
                        "AND e.strategy_id NOT IN (SELECT strategy_id FROM candidates) ORDER BY e.t_stat DESC LIMIT ?", (max(0, limit - len(A)),))
    A += [{**dict(r), "stage": "S2", "status": "near-miss (below promotion thresholds)", "label": "near-miss - UNQUALIFIED"} for r in near]
    B = []
    for r in conn.execute("SELECT c.strategy_id, c.stage, c.status, s.family, s.symbol, s.timeframe, s.agent, "
                          "(SELECT COUNT(*) FROM validations v WHERE v.strategy_id=c.strategy_id AND v.passed=1 AND v.stage IN "
                          "('walkforward','robustness','holdout','paper_forward')) gates_passed, "
                          "(SELECT json_extract(metrics_json,'$.t') FROM validations v WHERE v.strategy_id=c.strategy_id AND v.stage='walkforward' ORDER BY id DESC LIMIT 1) wf_t, "
                          "(SELECT json_extract(metrics_json,'$.exp_r') FROM validations v WHERE v.strategy_id=c.strategy_id AND v.stage='walkforward' ORDER BY id DESC LIMIT 1) wf_exp "
                          "FROM candidates c JOIN strategies s ON s.id=c.strategy_id ORDER BY gates_passed DESC, wf_t DESC LIMIT ?", (limit,)):
        B.append(dict(r))
    Cb = []
    for r in conn.execute("SELECT c.strategy_id, s.family, s.symbol, s.timeframe, c.stage, c.status FROM candidates c JOIN strategies s ON s.id=c.strategy_id "
                          "ORDER BY (SELECT COUNT(*) FROM validations v WHERE v.strategy_id=c.strategy_id AND v.passed=1) DESC, c.dev_t DESC LIMIT 25"):
        gs = compute(conn, r["strategy_id"])
        n_ok = sum(1 for x in gs if x["symbol"] == OK); n_part = sum(1 for x in gs if x["symbol"] == PART); n_fail = sum(1 for x in gs if x["symbol"] == FAIL)
        Cb.append({**dict(r), "met": n_ok, "partial": n_part, "failed": n_fail, "unknown": 15 - n_ok - n_part - n_fail,
                   "symbols": "".join(x["symbol"] for x in gs), "profit_progress": gs[6]["progress"]})
    Cb.sort(key=lambda x: (-x["met"], -x["partial"], x["failed"], -(x["profit_progress"] or 0)))
    return {"A": A, "B": B, "C": Cb[:limit]}
