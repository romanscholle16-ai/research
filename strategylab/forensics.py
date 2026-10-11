"""Failure forensics (Agent C): controlled counterfactual experiments on DEVELOPMENT data (train+validation; the locked test is never read).

For a candidate we re-run the same entries under single, controlled changes (no costs / half costs / doubled costs, other exits, one side only, session,
up- vs down-market windows) and name the most likely cause only when a counterfactual actually supports it.  Causes carry their evidence; diagnoses are
reproducible (deterministic backtests, no randomness) and stored in `failure_diagnoses` so later agents (repair, director) can use them."""
from __future__ import annotations
import copy, json, time
from dataclasses import replace
import numpy as np
from . import validation as V, strategies as ST, refine as RF
from .backtest import metrics

WARM = RF.WARM
SESSIONS = {"asia": (0, 8), "europe": (8, 16), "us": (16, 24)}


def _stats(tr):
    if len(tr) == 0: return dict(n=0, R=0.0, gross=0.0)
    return dict(n=int(len(tr)), R=round(float(tr.R.mean()), 4), gross=round(float(V.gross_R(tr)), 4))


def _free(em):
    return replace(em, taker_fee=0.0, maker_fee=0.0, half_spread=0.0, slippage=0.0, stop_slippage=0.0)


def counterfactuals(F, spec, em, hi, cache=None):
    cf = {}
    base = V.run(F, spec, em, WARM, hi, cache); cf["base"] = _stats(base)
    cf["no_cost"] = _stats(V.run(F, spec, _free(em), WARM, hi, None))
    cf["cost_x0.5"] = _stats(V.run(F, spec, em.with_cost_mult(0.5), WARM, hi, None))
    cf["cost_x2"] = _stats(V.run(F, spec, em.with_cost_mult(2.0), WARM, hi, None))
    for name, patch in (("stop_x0.7", dict(stop_atr=spec["stop_atr"] * 0.7)), ("stop_x1.5", dict(stop_atr=spec["stop_atr"] * 1.5)),
                        ("rr_2", dict(target_rr=2.0)), ("rr_4", dict(target_rr=4.0)), ("rr_none", dict(target_rr=0.0)),
                        ("hold_x0.5", dict(max_hold=max(2, int(spec["max_hold"] * 0.5)))), ("hold_x2", dict(max_hold=int(spec["max_hold"] * 2)))):
        s = copy.deepcopy(spec); s.update(patch)
        if ST.validate_spec(s): continue
        try: cf[name] = _stats(V.run(F, s, em, WARM, hi, None))
        except Exception: pass
    if len(base):
        for side, m in (("long", base.dir > 0), ("short", base.dir < 0)):
            cf["side_" + side] = dict(n=int(m.sum()), R=round(float(base.R[m].mean()), 4) if m.sum() >= 10 else None)
        hr = ((base.t_in % 86400) // 3600).astype(int)
        for k, (a, b) in SESSIONS.items():
            m = (hr >= a) & (hr < b); cf["session_" + k] = dict(n=int(m.sum()), R=round(float(base.R[m].mean()), 4) if m.sum() >= 30 else None)
        edges, te = RF.windows(F, hi)
        up_R, up_n, dn_R, dn_n = 0.0, 0, 0.0, 0
        for i in range(RF.K_WIN):
            w = base.slice_time(te[i], te[i + 1])
            if len(w) < RF.MIN_N: continue
            mk = F.c[min(edges[i + 1], F.n) - 1] / F.c[edges[i]] - 1
            if mk > 0.05: up_R += w.R.sum(); up_n += len(w)
            else: dn_R += w.R.sum(); dn_n += len(w)
        cf["up_market"] = dict(n=up_n, R=round(up_R / up_n, 4) if up_n else None); cf["down_market"] = dict(n=dn_n, R=round(dn_R / dn_n, 4) if dn_n else None)
        cf["stop_exit_frac"] = round(float((base.reason == 0).mean()), 3); cf["liq_dist_ratio"] = round(float(base.stop_frac.mean()), 5)
    return cf


def classify(cf, spec, weeks):
    """Evidence-backed causes. Each cause cites the counterfactual that supports it; no supported cause => 'no_edge' or 'inconclusive'."""
    causes, hints = [], []
    b = cf["base"]
    if b["n"] == 0: return [("insufficient_frequency", "no trades on development data")], ["widen_signal"]
    if b["n"] / max(weeks, 1) < 1.0: causes.append(("insufficient_frequency", f"{b['n'] / max(weeks, 1):.2f} trades/week")); hints.append("widen_signal")
    g, nc, n1 = cf["no_cost"]["R"], b["R"], b["R"]
    if g <= 0.01: causes.append(("no_predictive_edge", f"gross edge {g:+.3f}R even with zero costs")); return causes, hints
    if n1 <= 0 and cf["cost_x0.5"]["R"] > 0.005:
        causes.append(("fees_slippage", f"gross {g:+.3f}R, net {n1:+.3f}R, positive at half cost ({cf['cost_x0.5']['R']:+.3f}R)")); hints += ["wider_stop", "target_rr", "cost_filter"]
    alts = {k: v["R"] for k, v in cf.items() if k.startswith(("stop_x", "rr_", "hold_x")) and isinstance(v, dict) and v.get("n", 0) >= 0.6 * b["n"]}
    if alts:
        k, v = max(alts.items(), key=lambda kv: kv[1])
        if v > n1 + 0.03: causes.append(("exit_or_stop_problem", f"{k} improves net {n1:+.3f}R -> {v:+.3f}R")); hints.append("exit_" + k.split("_")[0] if k.startswith(("stop", "hold")) else "exit_target_rr")
    sl, ss = cf.get("side_long", {}).get("R"), cf.get("side_short", {}).get("R")
    if sl is not None and ss is not None and (sl - ss > 0.06 or ss - sl > 0.06):
        good = "long" if sl > ss else "short"; causes.append(("long_short_asymmetry", f"long {sl:+.3f}R vs short {ss:+.3f}R")); hints.append("side_" + good)
    ses = {k[8:]: v["R"] for k, v in cf.items() if k.startswith("session_") and isinstance(v, dict) and v.get("R") is not None}
    if len(ses) >= 2 and max(ses.values()) > 0.03 and min(ses.values()) < -0.02:
        causes.append(("wrong_session", "edge only in " + max(ses, key=ses.get) + f" ({ses})")); hints.append("session_filter")
    up, dn = cf.get("up_market", {}).get("R"), cf.get("down_market", {}).get("R")
    if up is not None and dn is not None and up > 0.02 and dn < -0.01: causes.append(("wrong_regime", f"up-market {up:+.3f}R vs flat/down {dn:+.3f}R")); hints.append("regime_filter")
    if cf.get("stop_exit_frac", 0) > 0.8 and cf.get("liq_dist_ratio", 1) < 0.003: causes.append(("liquidation_or_stop_noise", f"{cf['stop_exit_frac']:.0%} of trades stop out with a {cf['liq_dist_ratio']:.2%} stop")); hints.append("wider_stop")
    if not causes: causes.append(("inconclusive", f"net {n1:+.3f}R, gross {g:+.3f}R; no single-change counterfactual explains it"))
    return causes, hints


def diagnose(db, store, em, strategy_id, spec, symbol, tf, persist=True):
    it = store.get(symbol, tf); F = it["F"]; hi = it["bounds"]["val"][1]
    cf = counterfactuals(F, spec, em, hi); weeks = max((F.epoch_s[min(hi, F.n) - 1] - F.epoch_s[WARM]) / (7 * 86400), 1)
    causes, hints = classify(cf, spec, weeks)
    out = dict(causes=causes, hints=hints, cf=cf)
    if persist:
        db.x("INSERT INTO failure_diagnoses(ts,strategy_id,symbol,timeframe,primary_cause,causes_json,hints_json,cf_json) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), strategy_id, symbol, tf, causes[0][0], json.dumps(causes), json.dumps(hints), json.dumps(cf)))
    return out
