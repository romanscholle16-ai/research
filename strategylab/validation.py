"""Independent statistical validation + qualification pipeline.

Stages (each persisted; locked test period is evaluated at most once per strategy/instrument/timeframe):
  screen   : train period only          -> cheap rejection with failure reasons
  validate : validation period, walk-forward, WFO, parameter sensitivity, cost sensitivity, look-ahead test
  final    : risk sizing fit on train+val only; ONE-SHOT locked test; $300 Monte Carlo; multiple-testing
"""
from __future__ import annotations
import math
import numpy as np
from . import strategies as ST, capital as CAP
from .backtest import backtest, metrics, Trades, period_bounds, weekly_coverage
from .btcc import ExchangeModel, DEFAULT
from .features import Features

STATUS = ["REJECTED", "PROMISING", "BACKTEST_QUALIFIED", "PAPER_VALIDATED", "LIVE_ELIGIBLE"]


def norm_sf(z): return 0.5 * math.erfc(z / math.sqrt(2))


def run(F, spec, em, lo, hi, cache=None):
    key = ST.spec_hash(spec)
    if cache is not None and key in cache: L, S = cache[key]
    else:
        L, S = ST.signals(F, spec)
        if cache is not None: cache[key] = (L, S)
    return backtest(F, spec, L, S, em, lo, hi)


def gross_R(tr): return (tr.ret / np.maximum(tr.stop_frac, 1e-6)).mean() if len(tr) else 0.0


def screen(F, spec, bounds, em, min_trades):
    lo, hi = bounds["train"]
    tr = run(F, spec, em, lo, hi)
    m = metrics(tr); reasons = []
    years = max((F.epoch_s[hi - 1] - F.epoch_s[lo]) / (365.25 * 86400), 1e-6)
    m["trades_per_year"] = m["n"] / years
    if m["n"] < min_trades: reasons.append("too_few_trades")
    if m["n"] >= min_trades:
        if m["mean_R"] <= 0.03:
            g = gross_R(tr)
            reasons.append("cost_dominated" if g > 0.05 else "no_edge")
        elif m["t"] < 1.5: reasons.append("statistically_insignificant")
        if m["pf"] < 1.05: reasons.append("poor_risk_reward") if "no_edge" not in reasons else None
    m["gross_mean_R"] = float(gross_R(tr))
    return tr, m, sorted(set(r for r in reasons if r))


def lookahead_test(df, spec, cuts=(0.4, 0.7, 0.9), ref=None):
    """Recompute signals on data truncated at several cut points; signals before the cut must be identical."""
    n = len(df); L, S = ST.signals(Features(df, ref), spec)
    bad = 0; checked = 0
    for f in cuts:
        c = int(n * f)
        sub = df.iloc[:c]; r2 = ref.iloc[: ref.index.searchsorted(sub.index[-1], side="right")] if ref is not None else None
        L2, S2 = ST.signals(Features(sub, r2), spec)
        bad += int(np.sum(L[:c] != L2) + np.sum(S[:c] != S2)); checked += c
    return dict(mismatches=bad, checked=checked, passed=bad == 0)


def folds_consistency(F, spec, em, lo, hi, k=6):
    edges = np.linspace(lo, hi, k + 1).astype(int); res = []
    for a, b in zip(edges[:-1], edges[1:]):
        tr = run(F, spec, em, a, b); res.append((len(tr), float(tr.R.mean()) if len(tr) else 0.0))
    pos = sum(1 for n, r in res if n > 0 and r > 0)
    return dict(folds=res, positive=pos, k=k)


def walk_forward_opt(F, spec, em, lo, hi, k=6, rng=None):
    """Anchored WFO: for fold j>=2 pick the best of {base + neighbours} by in-sample mean R (all prior folds), trade it OOS."""
    cands = [spec] + ST.neighbors(spec, 10, rng)
    edges = np.linspace(lo, hi, k + 1).astype(int)
    sigs = [ST.signals(F, c) for c in cands]
    oos = []
    for j in range(2, k):
        ins = []
        for c, (L, S) in zip(cands, sigs):
            t = backtest(F, c, L, S, em, edges[0], edges[j]); ins.append(t.R.mean() if len(t) >= 20 else -9)
        b = int(np.argmax(ins)); L, S = sigs[b]
        oos.append(backtest(F, cands[b], L, S, em, edges[j], edges[j + 1]))
    allt = Trades.concat(oos)
    return dict(n=len(allt), mean_R=float(allt.R.mean()) if len(allt) else 0.0, t=metrics(allt)["t"] if len(allt) else 0.0)


def sensitivity(F, spec, em, lo, hi, rng):
    nb = ST.neighbors(spec, 10, rng); vals = []
    for s in nb:
        t = run(F, s, em, lo, hi); vals.append(float(t.R.mean()) if len(t) >= 20 else -1.0)
    base = run(F, spec, em, lo, hi); b = float(base.R.mean()) if len(base) else 0.0
    return dict(n_neighbors=len(vals), frac_positive=float(np.mean(np.array(vals) > 0)) if vals else 0.0,
                median=float(np.median(vals)) if vals else 0.0, base=b)


def placebo_test(F, spec, em, lo, hi, rng, n=100):
    """Random-entry benchmark: same side mix, same signal density, same stop/target/hold structure, same costs.
    Controls for market beta and exit-rule artefacts: p = P(random-entry mean R >= strategy mean R)."""
    L, S = ST.signals(F, spec)
    act = backtest(F, spec, L, S, em, lo, hi)
    if len(act) < 20: return dict(p=1.0, actual=0.0, placebo_mean=0.0, n=0)
    pl, ps = L[lo:hi].mean(), S[lo:hi].mean(); size = F.n; vals = []
    for _ in range(n):
        Lr = np.zeros(size, bool); Sr = np.zeros(size, bool)
        Lr[lo:hi] = rng.random(hi - lo) < pl; Sr[lo:hi] = rng.random(hi - lo) < ps
        Lr[:600] = False; Sr[:600] = False
        t = backtest(F, spec, Lr, Sr, em, lo, hi)
        if len(t) >= 10: vals.append(t.R.mean())
    a = float(act.R.mean())
    if len(vals) < n // 2: return dict(p=1.0, actual=a, placebo_mean=0.0, n=len(vals))
    p = (sum(v >= a for v in vals) + 1) / (len(vals) + 1)
    return dict(p=float(p), actual=a, placebo_mean=float(np.mean(vals)), n=len(vals))


def validate(F, spec, bounds, em, st, tr_train, rng):
    out = {}; reasons = []
    vlo, vhi = bounds["val"]; tlo, _ = bounds["train"]
    trv = run(F, spec, em, vlo, vhi); mv = metrics(trv); out["val"] = mv
    if mv["n"] < 20: reasons.append("too_few_trades_val")
    elif mv["mean_R"] <= 0 or mv["pf"] < 1.05: reasons.append("fails_out_of_sample")
    if reasons: return out, reasons, trv
    wf = folds_consistency(F, spec, em, tlo, vhi); out["folds"] = wf
    if wf["positive"] < math.ceil(wf["k"] * 2 / 3): reasons.append("regime_dependent_or_unstable")
    wo = walk_forward_opt(F, spec, em, tlo, vhi, rng=rng); out["wfo"] = wo
    if wo["n"] < 30 or wo["mean_R"] <= 0: reasons.append("fails_walk_forward")
    sens = sensitivity(F, spec, em, tlo, vhi, rng); out["sensitivity"] = sens
    if sens["n_neighbors"] and (sens["frac_positive"] < 0.6 or sens["median"] < 0.5 * max(sens["base"], 1e-9)): reasons.append("unstable_parameters_overfit")
    c15 = run(F, spec, em.with_cost_mult(1.5), tlo, vhi); out["cost_1.5x"] = float(c15.R.mean()) if len(c15) else 0.0
    c2 = run(F, spec, em.with_cost_mult(2.0), tlo, vhi); out["cost_2x"] = float(c2.R.mean()) if len(c2) else 0.0
    if out["cost_1.5x"] <= 0: reasons.append("cost_sensitive")
    la = lookahead_test(F.df, spec, ref=F.ref); out["lookahead"] = la
    if not la["passed"]: reasons.append("look_ahead_bias")
    if not reasons:
        pb = placebo_test(F, spec, em, tlo, vhi, rng); out["placebo"] = pb
        if pb["p"] > 0.15: reasons.append("no_alpha_vs_random_entries")
    return out, reasons, trv


def final_stage(F, spec, bounds, em, st, symbol, n_tested, tr_trainval):
    """One-shot locked-test + $300 capital simulation."""
    out = {}; reasons = []
    tlo, _ = bounds["train"]; _, vhi = bounds["val"]; elo, ehi = bounds["test"]
    t_tv0, t_tv1 = float(F.epoch_s[tlo]), float(F.epoch_s[vhi - 1])
    best, table = CAP.optimize_risk(tr_trainval, t_tv0, t_tv1, symbol, em, st.max_ruin_prob)
    if best is None: return out, ["insufficient_history_for_capital_sim"], None
    out["sizing_fit_on_train_val"] = best
    test = run(F, spec, em, elo, ehi); mt = metrics(test); out["test"] = mt
    t0, t1 = float(F.epoch_s[elo]), float(F.epoch_s[ehi - 1]) + 1
    wc = weekly_coverage(test, t0, t1); out["weekly_coverage_test"] = wc
    if mt["n"] < 20: reasons.append("too_few_trades_test")
    elif mt["mean_R"] <= 0 or mt["pf"] <= 1.0: reasons.append("fails_locked_test")
    if wc < st.min_trades_per_week_frac: reasons.append("inadequate_trade_frequency")
    cap_test = CAP.evaluate(test, t0, t1, symbol, best["risk"], best["lev"], em, best["margin"], paths=st.mc_paths, seed=11)
    alltr = Trades.concat([tr_trainval, test])
    cap_all = CAP.evaluate(alltr, t_tv0, t1, symbol, best["risk"], best["lev"], em, best["margin"], paths=st.mc_paths, seed=13)
    out["capital_test"] = cap_test; out["capital_all"] = cap_all
    for name, c in (("test", cap_test), ("all", cap_all)):
        if c is None: reasons.append("insufficient_history_for_capital_sim"); continue
        if c["monthly_withdraw_any"]["1000"]["p"] < st.min_target_prob: reasons.append("cannot_reach_target_" + name)
        if c["ruin"]["p"] > st.max_ruin_prob: reasons.append("excess_ruin_risk_" + name)
        if c["lose_50pct"]["p"] > st.max_lose50_prob: reasons.append("excess_drawdown_" + name)
    # multiple-testing safeguard: Sidak-adjusted p over every distinct strategy ever tested
    allm = metrics(alltr); z = allm["t"]; p = 2 * norm_sf(abs(z)) if z > 0 else 1.0
    adj = 1 - (1 - p) ** max(n_tested, 1)
    out["multiple_testing"] = dict(t=z, p=p, n_tested=n_tested, adj_p=adj)
    if adj > st.multiple_testing_alpha: reasons.append("fails_multiple_testing")
    return out, reasons, test


def qualification_status(stage_reasons: dict) -> str:
    if any(stage_reasons.get(k) for k in ("screen", "validate")): return "REJECTED"
    if stage_reasons.get("final") is None: return "PROMISING"
    return "BACKTEST_QUALIFIED" if not stage_reasons["final"] else "PROMISING" if all(
        r in ("fails_multiple_testing",) or r.startswith("cannot_reach_target") or r.startswith("excess_") or r == "inadequate_trade_frequency" for r in stage_reasons["final"]
    ) and not any(r in ("fails_locked_test", "too_few_trades_test") for r in stage_reasons["final"]) else "REJECTED"
