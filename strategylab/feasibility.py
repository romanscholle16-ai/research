"""Stage A: cheap eligibility + economic feasibility screen. Runs BEFORE any backtest or optimisation.

Counts COMPLETED TRADES (not candles): a signal is a trade slot only if the position is flat, so signals inside an open position,
inside the expected holding period, or while the opposite signal conflicts do not count. Two counts are produced:
  * upper bound  - minimum 2-bar spacing (entry next open, exit at earliest next bar); used for the hard frequency rejection
                   (reject only when even the optimistic count cannot reach the weekly minimum).
  * expected     - spacing = expected hold (max_hold/3, stops/targets usually exit earlier); used for the economics.
Economics for the $300 -> $1,000/month objective (50% of every positive month retained, so month-6 profit must reach $2,000):
  required monthly return r solves  r * E0 * (1+r)^(months-1) = withdraw/(1-retain)   (constant compounding, no earlier withdrawals).
  A trade risks at most rf (of equity) with leverage <= lev_cap; monthly return = N_month * rf_eff * E_net (E_net = net expectancy in R).
  Gross edge needed per trade  G = cost_R + r / (N_month * rf_eff)  where cost_R = round-trip costs / stop distance.
  Reject when G exceeds the largest believable edge (G_MAX R) or the leverage needed exceeds the cap. No fixed optimism: all inputs
  come from the candidate's own historical signals and the ExchangeModel costs (conservative defaults; unverified vs BTCC).
"""
from __future__ import annotations
import numpy as np
from . import strategies as ST

WEEK = 7 * 86400
RF_MAX = 0.10          # max risk per trade as a fraction of equity
LEV_CAP = 50.0         # sizing search never exceeds this
G_MAX = 0.80
G_HL = 2.0        # believable gross edge cap (R) in user-directed high-leverage mode (targets of 3-6R)           # gross edge per trade (in R) beyond which a rule is not believable
MIN_WEEKLY = 1.0       # hard mandatory minimum: completed trades per week on average
MAX_YEAR_SHARE = 0.60  # one calendar year may not contribute more than this share of the signals (needs >= 3 years)


def required_monthly_return(withdraw=1000.0, retain=0.5, months=6, start=300.0):
    target = withdraw / max(1.0 - retain, 1e-9); lo, hi = 0.0, 50.0
    for _ in range(80):
        r = (lo + hi) / 2
        if r * start * (1 + r) ** (months - 1) >= target: hi = r
        else: lo = r
    return hi


def spaced_count(idx: np.ndarray, gap: int) -> np.ndarray:
    """Greedy distinct slots from sorted signal indices with at least `gap` bars between consecutive accepted ones."""
    if len(idx) == 0: return idx
    keep = [idx[0]]; last = idx[0]
    for i in idx[1:]:
        if i - last >= gap: keep.append(i); last = i
    return np.asarray(keep)


def round_trip_cost(em):
    return em.round_trip()


def stage_a(F, spec, bounds, em, st, sig=None):
    """Returns dict(decision in {'pass','reject'}, reasons, failure_class, metrics...). `sig` optional precomputed (L,S)."""
    lo, hi = bounds["train"]
    L, S = sig if sig is not None else ST.signals(F, spec)
    both = L & S
    sig_idx = np.flatnonzero((L | S) & ~both)
    sig_idx = sig_idx[(sig_idx >= lo) & (sig_idx < hi - 2)]
    tradable = None
    if em.min_lev_capable and len(sig_idx):       # high-leverage rule: signals whose stop cannot fit inside the 250x liquidation distance (or is cost-dominated) are not tradable
        sf0 = spec["stop_atr"] * F.atr(spec.get("atr_n", 14))[sig_idx] / F.c[sig_idx]
        okm = em.tradable_stop(sf0); tradable = round(float(okm.mean()), 3); sig_idx = sig_idx[okm]
    span_w = max((F.epoch_s[hi - 1] - F.epoch_s[lo]) / WEEK, 1e-6)
    upper = spaced_count(sig_idx, 2)
    hold_est = max(2, int(spec["max_hold"] / 3))
    expected = spaced_count(sig_idx, hold_est)
    out = dict(signal_bars=int(len(sig_idx)), signal_bars_per_week=round(len(sig_idx) / span_w, 2),
               trades_week_upper=round(len(upper) / span_w, 3), trades_week_expected=round(len(expected) / span_w, 3),
               trades_month_expected=round(len(expected) / span_w * 52 / 12, 2), weeks=round(span_w, 1), hold_est_bars=hold_est)
    if tradable is not None: out["tradable_signal_frac"] = tradable
    reasons = []
    # --- frequency: completed trades per week on average, and not hostage to one period
    if out["trades_week_upper"] < MIN_WEEKLY: reasons.append("insufficient_frequency")
    years = (F.epoch_s[upper] // (365.25 * 86400)).astype(int) if len(upper) else np.array([], int)
    by_year = {}
    if len(years):
        for y in np.unique(years): by_year[int(1970 + y)] = round(float((years == y).sum()) / (len(upper)), 3)
    out["share_by_year"] = by_year
    if len(by_year) >= 3 and max(by_year.values()) > MAX_YEAR_SHARE: reasons.append("frequency_single_period")
    # --- economics
    atr = F.atr(spec.get("atr_n", 14))
    sfrac = (spec["stop_atr"] * atr[sig_idx] / F.c[sig_idx]) if len(sig_idx) else np.array([])
    sfrac = sfrac[np.isfinite(sfrac)]
    stop_med = float(np.median(sfrac)) if len(sfrac) else float("nan")
    c_rt = round_trip_cost(em)
    r_req = required_monthly_return(st.withdraw_target, st.retain_frac, st.horizon_months, st.start_equity)
    out.update(required_monthly_return=round(r_req, 3), round_trip_cost=round(c_rt, 5), stop_frac_median=round(stop_med, 5) if stop_med == stop_med else None)
    if stop_med == stop_med and stop_med > 0:
        cost_R = c_rt / stop_med
        lev_cap = em.max_leverage if getattr(em, 'high_leverage', False) else LEV_CAP; rf_max = 0.5 if getattr(em, 'high_leverage', False) else RF_MAX
        rf_eff = min(rf_max, 0.8 * lev_cap * stop_med)           # risk share is capped by the leverage ceiling
        lev_needed = rf_eff / stop_med
        n_m = max(out["trades_month_expected"], 1e-9)
        e_req = r_req / (n_m * rf_eff)
        g_need = cost_R + e_req
        out.update(cost_R=round(cost_R, 3), rf_eff=round(rf_eff, 4), leverage_needed=round(lev_needed, 1), required_net_expectancy_R=round(e_req, 3), required_gross_edge_R=round(g_need, 3))
        if getattr(em, 'min_lev_capable', 0) and stop_med > 0.8 * em.liq_distance(em.min_lev_capable): reasons.append("not_leverage_capable")
        hl = bool(getattr(em, 'min_lev_capable', 0)); g_cap = G_HL if hl else G_MAX; c_cap = 1.5 if hl else 0.5 * G_MAX     # high-leverage mode: wins are multiples of R (rr 3-6), so priors are loosened; backtest gates decide
        if cost_R > c_cap: reasons.append("cost_infeasible")                 # costs alone eat > 40% of the believable edge
        elif g_need > g_cap and "insufficient_frequency" not in reasons: reasons.append("infeasible_for_target")
        out["research_only"] = bool("infeasible_for_target" not in reasons and g_need > 0.6 * g_cap)   # passes, but only just
    elif "insufficient_frequency" not in reasons:
        reasons.append("no_signals")
    cls = classify(reasons)
    out.update(decision="reject" if reasons else "pass", reasons=reasons, failure_class=cls)
    return out


FAILURE_CLASS = {
    "insufficient_frequency": "frequency", "frequency_single_period": "frequency", "inadequate_trade_frequency": "frequency", "too_few_trades": "frequency",
    "too_few_trades_val": "frequency", "too_few_trades_test": "frequency", "no_signals": "frequency",
    "cost_infeasible": "costs", "cost_dominated": "costs", "cost_sensitive": "costs", "fill_or_stop_fragile": "costs",
    "infeasible_for_target": "economic_infeasibility", "cannot_reach_target_test": "economic_infeasibility", "cannot_reach_target_all": "economic_infeasibility",
    "excess_ruin_risk_test": "economic_infeasibility", "excess_ruin_risk_all": "economic_infeasibility", "excess_drawdown_test": "economic_infeasibility", "excess_drawdown_all": "economic_infeasibility",
    "no_edge": "no_edge", "statistically_insignificant": "no_edge", "poor_risk_reward": "no_edge", "no_alpha_vs_random_entries": "no_edge", "fails_out_of_sample": "no_edge", "fails_locked_test": "no_edge",
    "unstable_parameters_overfit": "instability", "regime_dependent_or_unstable": "instability", "fails_walk_forward": "instability", "single_regime_dependence": "instability", "depends_on_few_trades": "instability",
    "cost_infeasible_precheck": "costs", "not_leverage_capable": "costs", "noise_neighbourhood": "duplicate", "exit_variant_limit": "duplicate",
    "look_ahead_bias": "invalid", "fails_multiple_testing": "multiple_testing", "near_duplicate_failed_hypothesis": "duplicate",
}


def classify(reasons):
    if not reasons: return None
    order = ["invalid", "frequency", "costs", "economic_infeasibility", "no_edge", "instability", "multiple_testing", "duplicate"]
    classes = {FAILURE_CLASS.get(r, "other") for r in reasons}
    for c in order:
        if c in classes: return c
    return "other"


def signal_fingerprint(F, spec, lo, hi, sig=None):
    """Hash of the ENTRY signal positions in the research window: two specs with equal fingerprints trade the same entries
    (they differ only in exits/sizing) => tiny variations of one hypothesis."""
    import hashlib
    L, S = sig if sig is not None else ST.signals(F, spec)
    h = hashlib.sha1(); h.update(np.packbits(L[lo:hi]).tobytes()); h.update(b"|"); h.update(np.packbits(S[lo:hi]).tobytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- pre-signal checks (no signal computation, no backtest)
MAX_EXIT_VARIANTS = 3          # at most this many exit variants of one entry hypothesis
NEIGHBOUR_LIMIT = 3            # this many failed adjacent-grid neighbours => the neighbourhood is noise


def precheck_cost(F, spec, bounds, em):
    """Cost screen using only the timeframe's typical ATR: round-trip cost vs the stop distance this spec would use."""
    lo, hi = bounds["train"]; af = F.atr_frac_med(lo, hi, spec.get("atr_n", 14))
    if not (af == af and af > 0): return dict(ok=True, cost_R_est=None)
    c_rt = round_trip_cost(em); cost_R = c_rt / (spec["stop_atr"] * af)
    sf = spec["stop_atr"] * af
    if getattr(em, "min_lev_capable", 0) and sf > 0.8 * em.liq_distance(em.min_lev_capable):
        return dict(ok=False, reason="not_leverage_capable", cost_R_est=round(cost_R, 3), stop_frac_est=round(sf, 5))     # stop would sit beyond the liquidation price at 250x
    return dict(ok=cost_R <= (1.5 if getattr(em, 'min_lev_capable', 0) else 0.5 * G_MAX * 1.25), cost_R_est=round(cost_R, 3), stop_frac_est=round(sf, 5))


def hyp_keys(spec, symbol, tf):
    import hashlib, json
    ek = ST.entry_key(spec)
    hk = hashlib.sha1(f"{ek}|{symbol}|{tf}".encode()).hexdigest()[:16]
    gk = hashlib.sha1(json.dumps([spec["family"], symbol, tf, spec.get("side"), spec.get("filters") or []], sort_keys=True, default=list).encode()).hexdigest()[:16]
    return ek, hk, gk


def _adjacent(a, b, family):
    fam = ST.FAMILIES.get(family)
    if not fam or family == "gp_rule": return False
    diffs = [k for k in a if a.get(k) != b.get(k)]
    if len(diffs) != 1: return False
    g = fam["space"].get(diffs[0])
    return bool(g and a[diffs[0]] in g and b.get(diffs[0]) in g and abs(g.index(a[diffs[0]]) - g.index(b[diffs[0]])) == 1)


def novelty_check(db, spec, symbol, tf):
    """Registry-based novelty test BEFORE any signal/backtest work. Returns (ok, reason)."""
    import json
    ek, hk, gk = hyp_keys(spec, symbol, tf)
    r = db.q("SELECT n_variants FROM hyp_registry WHERE hkey=?", (hk,))
    if r and r[0]["n_variants"] >= MAX_EXIT_VARIANTS: return False, "exit_variant_limit"
    if spec["family"] != "gp_rule":
        rows = db.q("SELECT params_json, n_failed, best_stage FROM hyp_registry WHERE group_key=? AND hkey!=? AND n_failed>=1 AND best_stage=1", (gk, hk))
        near = sum(1 for x in rows if _adjacent(spec["params"], json.loads(x["params_json"]), spec["family"]))
        if near >= NEIGHBOUR_LIMIT: return False, "noise_neighbourhood"
    return True, None


def register(db, spec, symbol, tf, stage, failed):
    import json, time
    ek, hk, gk = hyp_keys(spec, symbol, tf); now = time.time()
    db.x("""INSERT INTO hyp_registry(hkey,entry_key,group_key,family,symbol,timeframe,side,params_json,n_variants,n_failed,best_stage,first_ts,last_ts)
            VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(hkey) DO UPDATE SET n_variants=n_variants+1, n_failed=n_failed+?, best_stage=MAX(best_stage,?), last_ts=?""",
         (hk, ek, gk, spec["family"], symbol, tf, spec.get("side"), json.dumps(spec["params"], sort_keys=True, default=list), int(failed), stage, now, now, int(failed), stage, now))
