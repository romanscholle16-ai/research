"""Portfolio construction & validation.

Selection uses ONLY train+validation-period trades. Because every strategy that reached the 'final' stage already
saw the locked test slice, component *eligibility* is defined by passing the train/val validation gates (not by test
outcome), and the portfolio is judged once on the locked test slice (one-shot per portfolio id).
Concurrent positions are risk-scaled (scale = 1/open-positions-at-entry) so total simultaneous risk stays bounded.
"""
from __future__ import annotations
import hashlib, json, time
import numpy as np
from . import capital as CAP, validation as V
from .backtest import Trades, metrics, weekly_coverage
from .btcc import DEFAULT


def concurrency_scale(tr: Trades) -> Trades:
    order = np.argsort(tr.t_in, kind="stable")
    tr = Trades(**{f: getattr(tr, f)[order] for f in Trades.FIELDS})
    t_in, t_out = tr.t_in, tr.t_out
    # open positions at entry i = #(entries <= t_in[i]) - #(exits <= t_in[i])   (includes itself)
    n_in = np.searchsorted(np.sort(t_in), t_in, side="right"); n_out = np.searchsorted(np.sort(t_out), t_in, side="right")
    scale = 1.0 / np.maximum(n_in - n_out, 1)
    tr.scale = scale
    return tr


def _weekly_series(tr, t0, t1):
    wk = 7 * 86400; w0, w1 = int(t0 // wk), int(t1 // wk)
    v = np.zeros(w1 - w0 + 1)
    for t, r in zip(tr.t_out, tr.R):
        i = int(t // wk) - w0
        if 0 <= i < len(v): v[i] += r
    return v


def load_pool(db, t_val_end, min_trades=30):
    rows = db.q("""SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, t.trades_json tj, s.family fam
                   FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id JOIN trades t ON t.strategy_id=ss.strategy_id AND t.symbol=ss.symbol AND t.timeframe=ss.timeframe AND t.stage='all'
                   WHERE ss.synthetic=0""")
    pool = []
    for r in rows:
        tr = Trades.from_json(json.loads(r["tj"])); tv = tr.slice_time(0, t_val_end)
        if len(tv) < min_trades: continue
        m = metrics(tv)
        if m["mean_R"] <= 0: continue
        pool.append(dict(sid=r["sid"], sy=r["sy"], tf=r["tf"], tr=tr, tv=tv, t=m["t"], fam=r["fam"]))
    pool.sort(key=lambda x: -x["t"])
    per, out = {}, []
    for c in pool:                              # diversity: best 8 per (family, instrument, timeframe)
        k = (c["fam"], c["sy"], c["tf"])
        if per.get(k, 0) < 8: per[k] = per.get(k, 0) + 1; out.append(c)
    return out


def build(pool, t0, t1, max_size=8, top=60):
    """Greedy forward selection on train+val: maximise combined t-stat + weekly coverage, penalise correlated components."""
    cands = pool[:top]; chosen = []; best_score = -9
    wk = {c["sid"] + c["sy"] + c["tf"]: _weekly_series(c["tv"], t0, t1) for c in cands}
    while len(chosen) < max_size:
        cur = None
        for c in cands:
            if c in chosen: continue
            k = c["sid"] + c["sy"] + c["tf"]
            if any(abs(np.corrcoef(wk[k], wk[x["sid"] + x["sy"] + x["tf"]])[0, 1]) > 0.7 for x in chosen if wk[k].std() > 0 and wk[x["sid"] + x["sy"] + x["tf"]].std() > 0): continue
            comb = concurrency_scale(Trades.concat([x["tv"] for x in chosen + [c]]))
            m = metrics(comb); sc = m["t"] + 4 * weekly_coverage(comb, t0, t1)
            if cur is None or sc > cur[0]: cur = (sc, c)
        if cur is None or cur[0] <= best_score + 0.05: break
        best_score = cur[0]; chosen.append(cur[1])
    return chosen, best_score


def run_portfolio(db, st, store, em=DEFAULT, hb=lambda: None):
    ref = store.get("BTCUSDT", "1h") if ("BTCUSDT", "1h") in store.available() else None
    if ref is None: return {"skipped": "no_reference_timeline"}
    F, b = ref["F"], ref["bounds"]
    t0 = float(F.epoch_s[b["train"][0]]); t_tr_end = float(F.epoch_s[b["val"][0]]); t_val_end = float(F.epoch_s[b["test"][0]]); t_end = float(F.epoch_s[-1]) + 1
    pool = load_pool(db, t_val_end)
    if len(pool) < 3: return {"skipped": "pool_too_small", "pool": len(pool)}
    chosen, score = build(pool, t0, t_val_end)
    if len(chosen) < 2: return {"skipped": "no_diversifying_combination"}
    comps = sorted((c["sid"], c["sy"], c["tf"]) for c in chosen)
    pid = "P-" + hashlib.sha1(json.dumps([comps, t_val_end]).encode()).hexdigest()[:10]
    if db.q("SELECT 1 FROM strategy_status WHERE strategy_id=? ", (pid,)): return {"skipped": "already_tested", "portfolio": pid}
    hb()
    tv = concurrency_scale(Trades.concat([c["tv"] for c in chosen]))
    test = concurrency_scale(Trades.concat([c["tr"].slice_time(t_val_end, 1e12) for c in chosen]))
    reasons = []; out = {"components": comps, "train_val_score": score}
    best, _ = CAP.optimize_risk(tv, t0, t_val_end, "BTCUSDT", em, st.max_ruin_prob)
    if best is None: return {"skipped": "insufficient_history"}
    out["sizing_fit_on_train_val"] = best
    mt = metrics(test); out["test"] = mt; wc = weekly_coverage(test, t_val_end, t_end); out["weekly_coverage_test"] = wc
    out["weekly_coverage_train_val"] = weekly_coverage(tv, t0, t_val_end)
    if mt["n"] < 20: reasons.append("too_few_trades_test")
    elif mt["mean_R"] <= 0 or mt["pf"] <= 1.0: reasons.append("fails_locked_test")
    if wc < st.min_trades_per_week_frac: reasons.append("inadequate_trade_frequency")
    alltr = Trades.concat([tv, test]); alltr = concurrency_scale(alltr)
    cap_t = CAP.evaluate(test, t_val_end, t_end, "BTCUSDT", best["risk"], best["lev"], em, best["margin"], paths=st.mc_paths, seed=21)
    cap_a = CAP.evaluate(alltr, t0, t_end, "BTCUSDT", best["risk"], best["lev"], em, best["margin"], paths=st.mc_paths, seed=23)
    out["capital_test"], out["capital_all"] = cap_t, cap_a
    for name, c in (("test", cap_t), ("all", cap_a)):
        if c is None: reasons.append("insufficient_history_for_capital_sim"); continue
        if c["monthly_withdraw_any"]["1000"]["p"] < st.min_target_prob: reasons.append("cannot_reach_target_" + name)
        if c["ruin"]["p"] > st.max_ruin_prob: reasons.append("excess_ruin_risk_" + name)
        if c["lose_50pct"]["p"] > st.max_lose50_prob: reasons.append("excess_drawdown_" + name)
    rb, rr = V.robustness(alltr); out["robustness"] = rb; reasons += rr
    n_tested = db.q("SELECT COUNT(*) n FROM strategies")[0]["n"]
    am = metrics(alltr); z = am["t"]; p = 2 * V.norm_sf(abs(z)) if z > 0 else 1.0
    out["multiple_testing"] = dict(t=z, p=p, n_tested=n_tested, adj_p=1 - (1 - p) ** max(n_tested, 1))
    if out["multiple_testing"]["adj_p"] > st.multiple_testing_alpha: reasons.append("fails_multiple_testing")
    status = V.qualification_status({"final": reasons})
    summary = {"portfolio": True, "synthetic": False, "final": out, "reasons": reasons, "screen": metrics(tv), "validate": {"val": metrics(tv)}}
    now = time.time()
    db.x("INSERT OR IGNORE INTO strategies(id,created,family,spec_json,spec_hash) VALUES(?,?,?,?,?)", (pid, now, "portfolio", json.dumps({"components": comps}), pid))
    db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,dataset_id,started,finished,status,metrics_json,failure_reasons,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
         (pid, "portfolio_final", "MULTI", "mix", "locked-test", now, now, "fail" if reasons else "pass", json.dumps({k: v for k, v in out.items() if k != "capital_all"}, default=float), json.dumps(reasons)))
    db.x("INSERT OR REPLACE INTO strategy_status(strategy_id,symbol,timeframe,status,updated,summary_json,synthetic) VALUES(?,?,?,?,?,?,0)",
         (pid, "MULTI", "mix", status, now, json.dumps(summary, default=float)))
    db.x("INSERT OR REPLACE INTO trades VALUES(?,?,?,?,?)", (pid, "MULTI", "mix", "all", json.dumps(alltr.to_json())))
    if status == "BACKTEST_QUALIFIED": db.log("info", "qualify", f"{pid} portfolio BACKTEST_QUALIFIED")
    return {"portfolio": pid, "status": status, "reasons": reasons, "components": len(comps)}
