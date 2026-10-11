"""Research-efficiency audit: what was tested, what it cost, why it failed, and whether legacy survivors are still viable.
Pure read-only analysis of the research DB (plus an optional write of per-strategy legacy verdicts). Never touches the locked test."""
from __future__ import annotations
import collections, json, time, statistics as S
import numpy as np
from . import feasibility as FE, strategies as ST
from .backtest import Trades
from .btcc import DEFAULT, get_model
from .config import Settings

FREQ = {"frequency"}; MONEY = {"no_edge", "costs", "economic_infeasibility", "instability"}


def _hyp_key(spec, symbol, tf):
    return json.dumps([spec.get("family"), spec.get("params"), spec.get("filters") or [], spec.get("side"), symbol, tf], sort_keys=True, default=list)


def _grid_neighbors(spec, others_by_group):
    """True if another tested hypothesis in the same group differs by exactly one adjacent grid step in exactly one parameter."""
    fam = ST.FAMILIES.get(spec["family"])
    if not fam: return False
    for o in others_by_group:
        diffs = [k for k in spec["params"] if spec["params"].get(k) != o["params"].get(k)]
        if len(diffs) != 1: continue
        k = diffs[0]; g = fam["space"].get(k)
        if g and spec["params"][k] in g and o["params"].get(k) in g and abs(g.index(spec["params"][k]) - g.index(o["params"][k])) == 1: return True
    return False


def run_audit(db, st: Settings | None = None, write_legacy=True):
    st = st or Settings.load(); em = get_model(st.exchange_preset); now = time.time(); out = {"generated": now}
    rows = db.q("""SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, ss.status stt, ss.summary_json sj, s.family fam, s.spec_json sp
                   FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.synthetic=0 AND ss.symbol!='MULTI'""")
    comp = {(r["strategy_id"], r["symbol"], r["timeframe"]): (r["t1"] - r["t0"]) for r in db.q(
        "SELECT strategy_id, symbol, timeframe, MIN(started) t0, MAX(finished) t1 FROM experiments WHERE synthetic=0 AND stage IN ('eligibility','screen','validate','final') GROUP BY 1,2,3")}
    has_elig = {(r["strategy_id"], r["symbol"], r["timeframe"]) for r in db.q("SELECT DISTINCT strategy_id, symbol, timeframe FROM experiments WHERE stage='eligibility' AND synthetic=0")}
    # ---- distinctness
    hyp = collections.defaultdict(list); sig_defs = set(); groups = collections.defaultdict(list)
    for r in rows:
        spec = json.loads(r["sp"]); k = _hyp_key(spec, r["sy"], r["tf"]); hyp[k].append(r["sid"]); sig_defs.add(json.dumps([spec["family"], spec["params"], spec.get("filters") or [], spec.get("side")], sort_keys=True, default=list))
        groups[(spec["family"], r["sy"], r["tf"], spec.get("side"), json.dumps(spec.get("filters") or [], sort_keys=True))].append(spec)
    seen = set(); near = 0
    for g, specs in groups.items():
        uniq = {json.dumps(s_["params"], sort_keys=True): s_ for s_ in specs}.values(); uniq = list(uniq)
        for s_ in uniq:
            if _grid_neighbors(s_, [o for o in uniq if o is not s_]): near += 1
    out["distinctness"] = dict(strategy_market_tests=len(rows), distinct_hypotheses=len(hyp), exit_only_duplicates=len(rows) - len(hyp),
                               distinct_entry_definitions=len(sig_defs), hypotheses_with_adjacent_grid_neighbour=near,
                               top_exit_variant_counts=sorted((len(v) for v in hyp.values()), reverse=True)[:5])
    # ---- failure reasons / classes
    reasons = collections.Counter(); classes = collections.Counter(); fam_stats = {}; tf_stats = {}
    for r in rows:
        sm = json.loads(r["sj"]); rs = sm.get("reasons", []) if r["stt"] == "REJECTED" or r["stt"] == "PROMISING" else []
        cls = FE.classify(sm.get("reasons", [])) if r["stt"] == "REJECTED" else ("promising" if r["stt"] == "PROMISING" else "other")
        classes[cls] += 1
        for x in sm.get("reasons", []): reasons[x] += 1
        c = comp.get((r["sid"], r["sy"], r["tf"]), 0.0)
        for D_, key in ((fam_stats, r["fam"]), (tf_stats, r["tf"])):
            a = D_.setdefault(key, dict(tests=0, secs=0.0, promising=0, validate_pass=0, screen_pass=0, freq_fail=0, cost_fail=0, noedge=0))
            a["tests"] += 1; a["secs"] += c; a["promising"] += r["stt"] == "PROMISING"; a["validate_pass"] += "validate" in sm; a["screen_pass"] += "screen" in sm
            cl = FE.classify(sm.get("reasons", [])) if r["stt"] == "REJECTED" else None
            a["freq_fail"] += cl == "frequency"; a["cost_fail"] += cl in ("costs", "economic_infeasibility"); a["noedge"] += cl == "no_edge"
    out["failure_reasons"] = reasons.most_common(25); out["failure_classes"] = dict(classes)
    tot = sum(a["secs"] for a in fam_stats.values()) or 1
    out["by_family"] = {k: dict(v, secs=round(v["secs"]), share=round(v["secs"] / tot, 3)) for k, v in sorted(fam_stats.items(), key=lambda kv: -kv[1]["secs"])}
    out["by_timeframe"] = {k: dict(v, secs=round(v["secs"]), share=round(v["secs"] / tot, 3)) for k, v in sorted(tf_stats.items(), key=lambda kv: -kv[1]["secs"])}
    out["families_without_meaningful_results"] = [k for k, v in fam_stats.items() if v["validate_pass"] == 0]
    out["timeframes_consuming_without_candidates"] = [k for k, v in tf_stats.items() if v["secs"] / tot > 0.05 and v["promising"] == 0]
    # ---- is the early filter executing before expensive work?
    post = [r for r in rows if (r["sid"], r["sy"], r["tf"]) in has_elig]; pre = [r for r in rows if (r["sid"], r["sy"], r["tf"]) not in has_elig]
    def waste(rs_):
        w = tot_ = 0.0
        for r in rs_:
            c = comp.get((r["sid"], r["sy"], r["tf"]), 0.0); tot_ += c
            sm = json.loads(r["sj"])
            if r["stt"] == "REJECTED" and FE.classify(sm.get("reasons", [])) in ("frequency", "costs", "economic_infeasibility", "duplicate"): w += c
        return round(w), round(tot_)
    wp, tp = waste(pre); wq, tq = waste(post)
    elig_before_screen = db.q("""SELECT COUNT(*) n FROM experiments e JOIN experiments s ON s.strategy_id=e.strategy_id AND s.symbol=e.symbol AND s.timeframe=e.timeframe
                                 WHERE e.stage='eligibility' AND e.status='fail' AND s.stage IN ('screen','validate','final') AND e.synthetic=0""")[0]["n"]
    out["early_filter"] = dict(rows_with_eligibility=len(post), rows_legacy_without=len(pre), rejected_at_eligibility=sum(1 for r in post if "screen" not in json.loads(r["sj"])),
                               rows_that_ran_backtest_after_failing_eligibility=elig_before_screen,
                               legacy_compute_wasted_on_infeasible_secs=wp, legacy_compute_total_secs=tp, legacy_waste_share=round(wp / max(tp, 1), 3),
                               current_compute_wasted_on_infeasible_secs=wq, current_compute_total_secs=tq, current_waste_share=round(wq / max(tq, 1), 3),
                               avg_secs_per_test_legacy=round(tp / max(len(pre), 1), 3), avg_secs_per_test_current=round(tq / max(len(post), 1), 3))
    # ---- legacy survivors vs current rules
    tr_rows = db.q("""SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, ss.summary_json sj, t.trades_json tj, s.spec_json sp FROM strategy_status ss
                      JOIN strategies s ON s.id=ss.strategy_id JOIN trades t ON t.strategy_id=ss.strategy_id AND t.symbol=ss.symbol AND t.timeframe=ss.timeframe AND t.stage='all'
                      WHERE ss.synthetic=0 AND ss.status='PROMISING' AND ss.symbol!='MULTI'""")
    c_rt = FE.round_trip_cost(em); r_req = FE.required_monthly_return(st.withdraw_target, st.retain_frac, st.horizon_months, st.start_equity)
    verd = collections.Counter(); viable = []; per_tf = collections.Counter()
    for r in tr_rows:
        sm = json.loads(r["sj"]); tr = Trades.from_json(json.loads(r["tj"]))
        if len(tr) < 5: verd["too_few_trades"] += 1; continue
        weeks = max((tr.t_out.max() - tr.t_in.min()) / FE.WEEK, 1e-6); per_week = len(tr) / weeks
        sf = float(np.median(tr.stop_frac)); cost_R = c_rt / max(sf, 1e-6); rf = min(FE.RF_MAX, 0.8 * FE.LEV_CAP * sf); e_req = r_req / max(per_week * 52 / 12 * rf, 1e-9); g = cost_R + e_req
        cov = sm.get("final", {}).get("weekly_coverage_test", 0)
        reasons_ = []
        if per_week < FE.MIN_WEEKLY: reasons_.append("insufficient_frequency")
        if cost_R > 0.5 * FE.G_MAX: reasons_.append("cost_infeasible")
        elif g > FE.G_MAX: reasons_.append("infeasible_for_target")
        if cov < st.min_trades_per_week_frac: reasons_.append("weekly_coverage_below_gate")
        legacy = dict(trades_per_week=round(per_week, 3), cost_R=round(cost_R, 3), required_gross_edge_R=round(g, 3), weekly_coverage_test=round(cov, 3), reasons=reasons_, viable=not reasons_)
        for x in reasons_: verd[x] += 1
        if not reasons_: viable.append((r["sid"], r["sy"], r["tf"]))
        per_tf[(r["tf"], "viable" if not reasons_ else "survivor_only")] += 1
        if write_legacy:
            sm["legacy_check"] = legacy
            db.x("UPDATE strategy_status SET summary_json=? WHERE strategy_id=? AND symbol=? AND timeframe=?", (json.dumps(sm, default=float), r["sid"], r["sy"], r["tf"]))
    out["legacy_promising"] = dict(checked=len(tr_rows), viable_under_current_stageA=len(viable), failure_counts=dict(verd), by_timeframe={f"{k[0]}:{k[1]}": v for k, v in per_tf.items()}, viable_ids=viable[:20])
    out["required_monthly_return"] = round(r_req, 3); out["round_trip_cost"] = round(c_rt, 5)
    db.x("CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, body TEXT)")
    db.x("INSERT INTO reports(ts,kind,body) VALUES(?,?,?)", (now, "efficiency", json.dumps(out, default=float)))
    return out


def render_markdown(o):
    d = o["distinctness"]; e = o["early_filter"]; L = o["legacy_promising"]
    md = ["# Research efficiency report", f"_generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(o['generated']))}; read-only audit of the research DB (history preserved)_", "",
          "## Distinctness", f"- strategy×market tests: **{d['strategy_market_tests']:,}**; genuinely distinct hypotheses (entry definition × market × timeframe × side): **{d['distinct_hypotheses']:,}**; "
          f"exit-only duplicates: **{d['exit_only_duplicates']:,}**; distinct entry definitions across markets: {d['distinct_entry_definitions']:,}",
          f"- hypotheses with an adjacent-grid-step neighbour (minor parameter variants): **{d['hypotheses_with_adjacent_grid_neighbour']:,}**; most exit variants of a single hypothesis: {d['top_exit_variant_counts']}", "",
          "## Compute by family (share of total experiment time)", "| family | tests | share | secs | screen pass | validate pass | promising | freq-fail | cost/econ-fail | no-edge |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in o["by_family"].items(): md.append(f"| {k} | {v['tests']:,} | {v['share']:.1%} | {v['secs']:,} | {v['screen_pass']} | {v['validate_pass']} | {v['promising']} | {v['freq_fail']} | {v['cost_fail']} | {v['noedge']} |")
    md += ["", f"Families with no validation-stage survivors: {', '.join(o['families_without_meaningful_results']) or 'none'}", "", "## Compute by timeframe", "| tf | tests | share | secs | validate pass | promising | freq-fail | cost/econ-fail |", "|---|---|---|---|---|---|---|---|"]
    for k, v in o["by_timeframe"].items(): md.append(f"| {k} | {v['tests']:,} | {v['share']:.1%} | {v['secs']:,} | {v['validate_pass']} | {v['promising']} | {v['freq_fail']} | {v['cost_fail']} |")
    md += ["", f"Timeframes using >5% of compute with zero promising candidates: {', '.join(o['timeframes_consuming_without_candidates']) or 'none'}", "", "## Why strategies fail",
           "| primary class | count |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in sorted(o["failure_classes"].items(), key=lambda kv: -kv[1])]
    md += ["", "Top individual reasons (a strategy can carry several): " + ", ".join(f"{k} {v:,}" for k, v in o["failure_reasons"][:12]), "",
           "## Is the early filter running before expensive work?",
           f"- rows with an eligibility stage: {e['rows_with_eligibility']:,} (legacy rows without: {e['rows_legacy_without']:,}); rejected at eligibility without any backtest: {e['rejected_at_eligibility']:,}",
           f"- rows that ran a backtest AFTER failing eligibility: **{e['rows_that_ran_backtest_after_failing_eligibility']}** (must be 0)",
           f"- compute wasted on frequency/cost/infeasible/duplicate outcomes: legacy {e['legacy_compute_wasted_on_infeasible_secs']:,}s of {e['legacy_compute_total_secs']:,}s (**{e['legacy_waste_share']:.1%}**) vs current {e['current_compute_wasted_on_infeasible_secs']:,}s of {e['current_compute_total_secs']:,}s (**{e['current_waste_share']:.1%}**)",
           f"- average seconds per test: legacy {e['avg_secs_per_test_legacy']}s, current {e['avg_secs_per_test_current']}s", "",
           "## Are the PROMISING candidates still viable under current rules?",
           f"- checked {L['checked']:,} PROMISING candidates (from stored trades); **{L['viable_under_current_stageA']}** pass the current frequency + economic screen. Failures: {L['failure_counts']}",
           f"- by timeframe: {L['by_timeframe']}", f"- target arithmetic: +{o['required_monthly_return']:.0%}/month sustained; round-trip cost {o['round_trip_cost']:.4%} of notional (conservative BTCC assumption, unverified)"]
    return "\n".join(md) + "\n"
