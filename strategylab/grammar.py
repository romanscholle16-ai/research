"""Constrained strategy grammar + genetic programming (zero-cost, interpretable, reproducible).

A gp_rule strategy is  side in {long, short}  and  rule = AND of 1-3 atoms.  Every atom is a CAUSAL boolean condition on features known
at bar close (price momentum, z-score, RSI, efficiency-ratio regime, volatility percentile, taker-flow z, VWAP deviation, trend/HTF
filters, breakouts, session, relative value, funding).  Atoms carry a threshold from a fixed grid, so the search space is finite,
every rule is human-readable ("long when ret12>1.0 AND er20>0.3 AND htf48_above"), and identical rules hash identically (dedupe).
Variation operators: threshold step, atom replace/add/drop, crossover.  Diversity: elites are bucketed into niches (feature-set
signature x market x timeframe) and capped per niche.  Atom sampling is biased by a TRAIN-PERIOD-ONLY univariate screen; selection of
strategies is still made solely by the full validation pipeline (the screen only orders what to try first)."""
from __future__ import annotations
import copy, hashlib, json
import numpy as np

MAX_ATOMS = 3
RET_N = (3, 12, 48, 144); Z_N = (20, 60, 200); ER_N = (20, 60); IMB_N = (1, 6, 24); BRK_N = (20, 60, 200); EMA_N = (50, 200); HTF_M = (12, 48, 288)
SESSIONS = {0: list(range(0, 8)), 1: list(range(8, 16)), 2: list(range(16, 24))}

# name -> (threshold grid, requires)
ATOMS: dict[str, tuple[list, str]] = {}
for n in RET_N:
    ATOMS[f"ret{n}_gt"] = ([0.25, 0.5, 1.0, 1.5], ""); ATOMS[f"ret{n}_lt"] = ([0.25, 0.5, 1.0, 1.5], "")
for n in Z_N:
    ATOMS[f"z{n}_gt"] = ([1.0, 1.5, 2.0, 2.5], ""); ATOMS[f"z{n}_lt"] = ([1.0, 1.5, 2.0, 2.5], "")
ATOMS["rsi14_gt"] = ([60, 70, 80], ""); ATOMS["rsi14_lt"] = ([40, 30, 20], "")
for n in ER_N:
    ATOMS[f"er{n}_gt"] = ([0.2, 0.3, 0.4], ""); ATOMS[f"er{n}_lt"] = ([0.2, 0.3, 0.4], "")
ATOMS["atrpct_gt"] = ([0.3, 0.5, 0.7], ""); ATOMS["atrpct_lt"] = ([0.3, 0.5, 0.7], "")
for n in IMB_N:
    ATOMS[f"imbz{n}_gt"] = ([1.0, 1.5, 2.0], "taker"); ATOMS[f"imbz{n}_lt"] = ([1.0, 1.5, 2.0], "taker")
ATOMS["vwapdev_gt"] = ([0.5, 1.0, 2.0], ""); ATOMS["vwapdev_lt"] = ([0.5, 1.0, 2.0], "")
for n in EMA_N:
    ATOMS[f"ema{n}_above"] = ([0], ""); ATOMS[f"ema{n}_below"] = ([0], "")
for m in HTF_M:
    ATOMS[f"htf{m}_above"] = ([0], ""); ATOMS[f"htf{m}_below"] = ([0], "")
for n in BRK_N:
    ATOMS[f"brk{n}_up"] = ([0], ""); ATOMS[f"brk{n}_down"] = ([0], "")
ATOMS["session_in"] = ([0, 1, 2], ""); ATOMS["volz_gt"] = ([1.5, 2.5], "")
ATOMS["ratioz_gt"] = ([1.5, 2.0], "ref"); ATOMS["ratioz_lt"] = ([1.5, 2.0], "ref")
ATOMS["fund_gt"] = ([0.8, 0.9], "funding"); ATOMS["fund_lt"] = ([0.2, 0.1], "funding")


def atom_array(F, name: str, thr) -> np.ndarray:
    """Evaluate one atom -> boolean array (NaN comparisons are False). Strictly causal."""
    c = F.c
    def cmp(a, op, t): 
        with np.errstate(invalid="ignore"): return (a > t) if op == "gt" else (a < t)
    if name.startswith("ret"):
        n = int(name[3:].split("_")[0]); lim = thr * F.atr(14) / c * np.sqrt(n)
        r = F.ret(n); return (r > lim) if name.endswith("_gt") else (r < -lim)
    if name.startswith("z") and "_" in name:
        n = int(name[1:].split("_")[0]); z = F.zscore(n); return cmp(z, name[-2:], thr) if name.endswith("gt") else cmp(z, "lt", -thr)
    if name.startswith("rsi14"): return cmp(F.rsi(14), name[-2:], thr)
    if name.startswith("er"):
        n = int(name[2:].split("_")[0]); return cmp(F.efficiency_ratio(n), name[-2:], thr)
    if name.startswith("atrpct"): return cmp(F.atr_pct(14, 500), name[-2:], thr)
    if name.startswith("imbz"):
        n = int(name[4:].split("_")[0]); z = F.taker_imb_z(n); return cmp(z, "gt", thr) if name.endswith("gt") else cmp(z, "lt", -thr)
    if name.startswith("vwapdev"): d = F.vwap_dev(); return cmp(d, "gt", thr) if name.endswith("gt") else cmp(d, "lt", -thr)
    if name.startswith("ema"):
        n = int(name[3:].split("_")[0]); e = F.ema(n); return cmp(c - e, "gt" if name.endswith("above") else "lt", 0)
    if name.startswith("htf"):
        m = int(name[3:].split("_")[0]); e = F.htf_ema(m, 20); return cmp(c - e, "gt" if name.endswith("above") else "lt", 0)
    if name.startswith("brk"):
        n = int(name[3:].split("_")[0]); return (c > F.hh(n)) if name.endswith("up") else (c < F.ll(n))
    if name == "session_in": return np.isin(F.hour, SESSIONS[int(thr)])
    if name == "volz_gt": return cmp(F.vol_z(50), "gt", thr)
    if name.startswith("ratioz"): z = F.ratio_z(250); return cmp(z, "gt", thr) if name.endswith("gt") else cmp(z, "lt", -thr)
    if name.startswith("fund"): p = F.fund_pct(500); return cmp(p, "gt", thr) if name.endswith("gt") else cmp(p, "lt", thr)
    raise KeyError(name)


def rule_signals(F, rule, side):
    cond = np.ones(F.n, bool)
    for name, thr in rule: cond &= atom_array(F, name, thr)
    z = np.zeros(F.n, bool)
    return (cond, z) if side == "long" else (z, cond)


def validate_rule(rule, side) -> list[str]:
    errs = []
    if side not in ("long", "short"): errs.append("gp_rule needs side long or short")
    if not isinstance(rule, list) or not (1 <= len(rule) <= MAX_ATOMS): return errs + ["rule needs 1-3 atoms"]
    seen = set()
    for a in rule:
        if not (isinstance(a, (list, tuple)) and len(a) == 2 and a[0] in ATOMS and a[1] in ATOMS[a[0]][0]): errs.append(f"bad atom {a}"); continue
        if a[0] in seen: errs.append("duplicate atom")
        seen.add(a[0])
    return errs


def available(name, has_taker=True, has_funding=True, has_ref=True):
    req = ATOMS[name][1]
    return not ((req == "taker" and not has_taker) or (req == "funding" and not has_funding) or (req == "ref" and not has_ref))


def describe(rule, side):
    return f"{side} when " + " AND ".join(f"{n}{'' if ATOMS[n][0] == [0] else '>=' if False else ''}({t})" if ATOMS[n][0] != [0] else n for n, t in rule)


def rule_key(rule) -> str:
    return "|".join(sorted(n for n, _ in rule))


def niche(rule, symbol, tf) -> str:
    return f"{rule_key(rule)}@{symbol}:{tf}"


# ------------------------------------------------------------------ variation operators
def random_rule(rng, side, weights: dict | None = None, k=None, avail=None):
    names = [n for n in ATOMS if (avail is None or avail(n))]
    w = np.array([max((weights or {}).get(n, 0.0), 0.0) + 0.5 for n in names]); w = w / w.sum()
    k = k or int(rng.choice([1, 2, 3], p=[0.2, 0.5, 0.3])); chosen = []
    for _ in range(k * 6):
        n = names[int(rng.choice(len(names), p=w))]
        if n not in [c[0] for c in chosen] and not _contradict(n, [c[0] for c in chosen]): chosen.append([n, ATOMS[n][0][int(rng.integers(len(ATOMS[n][0])))]])
        if len(chosen) == k: break
    return sorted(chosen)


def _contradict(n, others):
    """Cheap grammar constraint: forbid opposite-direction atoms of the same feature (e.g. ret12_gt with ret12_lt)."""
    base = n.rsplit("_", 1)[0]; pairs = {"gt": "lt", "lt": "gt", "above": "below", "below": "above", "up": "down", "down": "up"}
    for o in others:
        ob, osuf = o.rsplit("_", 1)
        if ob == base and pairs.get(n.rsplit("_", 1)[1]) == osuf: return True
    return False


def mutate(rule, rng, weights=None, avail=None):
    r = copy.deepcopy(rule); move = rng.choice(["step", "replace", "add", "drop"], p=[.4, .25, .2, .15])
    if move == "step" or (move == "drop" and len(r) == 1) or (move == "add" and len(r) >= MAX_ATOMS):
        i = int(rng.integers(len(r))); g = ATOMS[r[i][0]][0]; j = g.index(r[i][1]); j = min(max(j + int(rng.choice([-1, 1])), 0), len(g) - 1); r[i][1] = g[j]
    elif move == "replace":
        i = int(rng.integers(len(r))); new = random_rule(rng, "long", weights, 1, avail)[0]
        if new[0] not in [x[0] for k, x in enumerate(r) if k != i]: r[i] = new
    elif move == "add":
        new = random_rule(rng, "long", weights, 1, avail)[0]
        if new[0] not in [x[0] for x in r] and not _contradict(new[0], [x[0] for x in r]): r.append(new)
    else: r.pop(int(rng.integers(len(r))))
    return sorted(r)


def crossover(a, b, rng):
    pool = [x for x in a + b]; rng.shuffle(pool); out = []
    for n, t in pool:
        if n not in [o[0] for o in out] and not _contradict(n, [o[0] for o in out]): out.append([n, t])
        if len(out) == min(MAX_ATOMS, int(rng.integers(1, MAX_ATOMS + 1))): break
    return sorted(out) or sorted(a)


# ------------------------------------------------------------------ train-only univariate screen
def atom_screen(F, lo, hi, horizon=12, min_n=60):
    """Mean forward return (t-stat) when each atom is true, TRAIN period only. Returns {name: best t over thresholds, oriented for long}.
    Used only to order exploration; every strategy still has to survive the full validation pipeline."""
    c = F.c; fwd = np.full(F.n, np.nan); fwd[:-horizon] = c[horizon:] / c[:-horizon] - 1
    sl = slice(lo, hi - horizon); out = {}
    for name, (grid, _) in ATOMS.items():
        best = 0.0
        for thr in grid:
            try: m = atom_array(F, name, thr)[sl]
            except Exception: continue
            x = fwd[sl][m & np.isfinite(fwd[sl])]
            if len(x) < min_n: continue
            t = x.mean() / (x.std() / np.sqrt(len(x)) + 1e-12)
            if abs(t) > abs(best): best = float(t)
        out[name] = best
    return out
