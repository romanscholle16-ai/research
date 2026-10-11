"""Declarative strategy specs -> causal entry signals. Each family carries a research rationale and a falsifiable claim."""
from __future__ import annotations
import hashlib, json, itertools
import numpy as np

EXIT_SPACE = {"stop_atr": [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0], "target_rr": [0, 1.0, 1.5, 2.0, 3.0], "max_hold": [12, 24, 48, 96, 192, 288], "atr_n": [14]}   # wide stops/long holds: only way short timeframes can clear costs
SIDES = ["long", "short", "both"]


def _nz(a):
    return np.nan_to_num(a, nan=0.0)


def sig_donchian(F, p):
    up = F.c > F.hh(p["n"]); dn = F.c < F.ll(p["n"])
    ef = F.ema(p["trend_n"]) if p["trend_n"] else None
    if ef is not None: up &= F.c > ef; dn &= F.c < ef
    return up, dn


def sig_tsmom(F, p):
    r = F.ret(p["lookback"]); thr = p["thr_atr"] * F.atr(14) / F.c * np.sqrt(p["lookback"])
    return r > thr, r < -thr


def sig_zrev(F, p):
    z = F.zscore(p["n"])
    return z < -p["k"], z > p["k"]


def sig_rsi(F, p):
    r = F.rsi(p["n"]); e = F.ema(p["trend_n"]) if p["trend_n"] else None
    L = r < p["lo"]; S = r > 100 - p["lo"]
    if e is not None: L &= F.c > e; S &= F.c < e  # buy dips in uptrends only
    return L, S


def sig_squeeze(F, p):
    bw = F.bandwidth(20); lowbw = F.pct_rank(("bw", 20), bw, p["w"]) < p["pct"]
    was = np.zeros(F.n, bool); sq = np.nan_to_num(lowbw.astype(float)) > 0
    for k in range(1, p["lookback"] + 1): was[k:] |= sq[:-k]
    return was & (F.c > F.hh(p["brk"])), was & (F.c < F.ll(p["brk"]))


def sig_sweep(F, p):
    """Liquidity sweep: wick takes out prior n-bar extreme but close recovers back inside (stop-run reversal)."""
    hh, ll = F.hh(p["n"]), F.ll(p["n"]); rng = np.maximum(F.h - F.l, 1e-12)
    bear = (F.h > hh) & (F.c < hh) & ((F.h - np.maximum(F.o, F.c)) / rng > p["wick"])
    bull = (F.l < ll) & (F.c > ll) & ((np.minimum(F.o, F.c) - F.l) / rng > p["wick"])
    return bull, bear


def sig_fvg(F, p):
    """ICT fair value gap: 3-candle imbalance; enter when price retests the gap, with a trend filter. Fully vectorised."""
    import pandas as pd
    n = F.n; h, l, c = F.h, F.l, F.c
    min_gap = np.nan_to_num(p["min_gap_atr"] * F.atr(14))
    bull = np.zeros(n, bool); bear = np.zeros(n, bool)
    bull[2:] = (l[2:] > h[:-2]) & ((l[2:] - h[:-2]) > min_gap[2:]) & (min_gap[2:] > 0)     # gap [h[i-2], l[i]] known at close i
    bear[2:] = (h[2:] < l[:-2]) & ((l[:-2] - h[2:]) > min_gap[2:]) & (min_gap[2:] > 0)
    bl = np.where(bull, np.r_[np.nan, np.nan, h[:-2]], np.nan); bh = np.where(bull, l, np.nan)
    sl_ = np.where(bear, h, np.nan); sh = np.where(bear, np.r_[np.nan, np.nan, l[:-2]], np.nan)
    hold = lambda a: pd.Series(a).ffill(limit=p["expiry"]).to_numpy()
    sh1 = lambda a: np.r_[np.nan, a[:-1]]                                                   # usable from the NEXT bar
    bl, bh, sl_, sh = (sh1(hold(x)) for x in (bl, bh, sl_, sh))
    ef = F.ema(p["trend_n"])
    L = (F.l <= bh) & (F.c > bl) & (F.c > F.o) & (F.c > ef)
    S = (F.h >= sl_) & (F.c < sh) & (F.c < F.o) & (F.c < ef)
    return L, S


def sig_bos(F, p):
    """SMC break of structure then pullback entry: close above last confirmed swing high, then return toward broken level."""
    import pandas as pd
    swh, swl = F.swing_high(p["k"]), F.swing_low(p["k"])
    bos_up = (F.c > np.r_[np.nan, swh[:-1]]) ; bos_dn = (F.c < np.r_[np.nan, swl[:-1]])
    lvl_up = pd.Series(np.where(bos_up, np.r_[np.nan, swh[:-1]], np.nan)).ffill(limit=p["window"]).to_numpy()
    lvl_dn = pd.Series(np.where(bos_dn, np.r_[np.nan, swl[:-1]], np.nan)).ffill(limit=p["window"]).to_numpy()
    sh = lambda a: np.r_[np.nan, a[:-1]]
    L = (F.l <= sh(lvl_up) * (1 + 0.0) + p["tol_atr"] * F.atr(14)) & (F.c > sh(lvl_up)) & (F.c > F.o) & ~bos_up
    S = (F.h >= sh(lvl_dn) - p["tol_atr"] * F.atr(14)) & (F.c < sh(lvl_dn)) & (F.c < F.o) & ~bos_dn
    return np.nan_to_num(L.astype(float)) > 0, np.nan_to_num(S.astype(float)) > 0


def sig_failbreak(F, p):
    """Momentum exhaustion: price closes beyond the n-bar extreme, then closes back inside within k bars -> fade the failed move."""
    hh, ll = F.hh(p["n"]), F.ll(p["n"]); brk_up = F.c > hh; brk_dn = F.c < ll
    L = np.zeros(F.n, bool); S = np.zeros(F.n, bool)
    for k in range(1, p["k"] + 1):
        up_k = np.r_[np.zeros(k, bool), brk_up[:-k]]; dn_k = np.r_[np.zeros(k, bool), brk_dn[:-k]]
        hh_k = np.r_[np.full(k, np.nan), hh[:-k]]; ll_k = np.r_[np.full(k, np.nan), ll[:-k]]
        S |= up_k & (F.c < hh_k); L |= dn_k & (F.c > ll_k)
    return L, S


def sig_funding(F, p):
    pr = F.fund_pct(p["w"])
    return pr < p["pct"], pr > 1 - p["pct"]   # crowded shorts -> long; crowded longs -> short


def sig_taker(F, p):
    imb = F.taker_imb(p["n"]); z = (imb - pd_roll(imb, p["w"], "mean")) / pd_roll(imb, p["w"], "std")
    r = F.ret(p["n"])
    if p["mode"] == "divergence":   # price down but aggressive buying dominates (absorption) -> long
        return (z > p["k"]) & (r < 0), (z < -p["k"]) & (r > 0)
    return (z > p["k"]) & (r > 0), (z < -p["k"]) & (r < 0)  # continuation


def pd_roll(a, w, how):
    import pandas as pd
    s = pd.Series(a).rolling(w, min_periods=w // 2)
    return (s.mean() if how == "mean" else s.std()).to_numpy()


def sig_absorb(F, p):
    """High volume, small range, close near the opposite extreme => absorption; fade in direction of absorbed flow."""
    rng = np.maximum(F.h - F.l, 1e-12); body = abs(F.c - F.o) / rng
    hv = F.vol_z(50) > p["vz"]; small = (rng < p["rng_atr"] * F.atr(14)) & (body < 0.4)
    imb = F.taker_imb(1)
    return hv & small & (imb < -p["imb"]), hv & small & (imb > p["imb"])  # sellers absorbed => long


def sig_session(F, p):
    """Calendar/time-of-day drift. hours are fitted on TRAIN data only by the inventor (stored in spec)."""
    m = np.isin(F.hour, p["hours"]) & (F.minute == 0)
    return m & (p.get("dir", 1) > 0), m & (p.get("dir", 1) < 0)


def sig_volreg(F, p):
    pr = F.atr_pct(14, p["w"]); e = F.ema(p["n"]); cond = (pr > p["lo"]) if p["mode"] == "high" else (pr < p["lo"])
    return cond & (F.c > e) & (F.c > np.r_[np.nan, F.c[:-1]]), cond & (F.c < e) & (F.c < np.r_[np.nan, F.c[:-1]])


def sig_leadlag(F, p):
    rr = F.ref_ret(p["n"]); own = F.ret(p["n"])
    thr = p["thr_atr"] * F.atr(14) / F.c
    return (rr > thr) & (own < rr * 0.5), (rr < -thr) & (own > rr * 0.5)


def sig_vwap(F, p):
    d = F.vwap_dev(); ok = F.hour >= p["min_hour"]   # let the session VWAP mature before fading
    return (d < -p["k"]) & ok, (d > p["k"]) & ok


def sig_nr(F, p):
    """Crabel narrow-range: after the narrowest range of n bars, trade the break of that bar's extreme."""
    import pandas as pd
    rng = F.h - F.l; nr = rng == pd.Series(rng).rolling(p["n"]).min().to_numpy()
    sh = lambda a: np.r_[np.zeros(1, a.dtype), a[:-1]]
    prev_nr = sh(nr); ph, pl = np.r_[np.nan, F.h[:-1]], np.r_[np.nan, F.l[:-1]]
    return prev_nr & (F.c > ph), prev_nr & (F.c < pl)


def sig_ratio(F, p):
    z = F.ratio_z(p["n"])
    return z < -p["k"], z > p["k"]


def sig_gp(F, p):
    from . import grammar
    return grammar.rule_signals(F, p["rule"], p["side"])


FAMILIES = {
    "donchian_breakout": dict(fn=sig_donchian, space={"n": [20, 40, 80, 160], "trend_n": [0, 100, 200]}, requires=[],
        text="Range breakouts continue because stop clusters beyond ranges trigger forced order flow.",
        why="Breakout stops and momentum ignition create short-horizon serial correlation after range expansion.",
        falsify="Net expectancy per trade <= 0 after costs, or t-stat insignificant out-of-sample."),
    "tsmom": dict(fn=sig_tsmom, space={"lookback": [12, 24, 48, 96], "thr_atr": [0.25, 0.5, 1.0]}, requires=[],
        text="Time-series momentum: past return sign predicts next-period return in crypto.",
        why="Slow information diffusion + herding documented in time-series momentum literature.",
        falsify="Sign of lookback return has no predictive power OOS."),
    "zscore_meanrev": dict(fn=sig_zrev, space={"n": [20, 50, 100], "k": [1.5, 2.0, 2.5, 3.0]}, requires=[],
        text="Short-horizon overreaction reverts toward the local mean.",
        why="Liquidity-provision premia: price pressure from forced flow mean-reverts as inventory is absorbed.",
        falsify="Extreme z-scores are followed by continuation (negative net expectancy)."),
    "rsi_trend_dip": dict(fn=sig_rsi, space={"n": [7, 14], "lo": [20, 25, 30, 35], "trend_n": [0, 100, 200]}, requires=[],
        text="Buying oversold dips only within the dominant trend (and mirrored for shorts) outperforms unconditional reversion.",
        why="Trend filter removes catching-falling-knives regimes; pullbacks in trends carry liquidity premium.",
        falsify="Filtered dips do not beat unfiltered; net expectancy <= 0."),
    "vol_squeeze_breakout": dict(fn=sig_squeeze, space={"w": [250, 500], "pct": [0.1, 0.2], "lookback": [3, 6, 12], "brk": [10, 20]}, requires=[],
        text="Volatility compression precedes expansion; breakout direction after a squeeze is tradable.",
        why="Volatility clusters and mean-reverts (GARCH effects); low-vol states are followed by large moves.",
        falsify="Post-squeeze breakouts show no larger directional follow-through than random breakouts."),
    "liquidity_sweep_reversal": dict(fn=sig_sweep, space={"n": [20, 40, 80], "wick": [0.4, 0.5, 0.6]}, requires=[],
        text="Wicks that take prior highs/lows then close back inside indicate stop-runs; price reverses.",
        why="Stop liquidity above/below swing points is harvested by large participants; absence of follow-through marks exhaustion (SMC/ICT liquidity sweep).",
        falsify="Sweep-and-reclaim bars show no reversal drift vs. all bars."),
    "ict_fvg_retest": dict(fn=sig_fvg, space={"min_gap_atr": [0.2, 0.5, 1.0], "expiry": [6, 12, 24], "trend_n": [50, 100, 200]}, requires=[],
        text="Fair value gaps formed by displacement act as support/resistance on retest in trend direction.",
        why="Displacement leaves unfilled resting orders; retests attract the remaining liquidity (ICT FVG).",
        falsify="Retested gaps hold no more often than random price levels."),
    "smc_bos_pullback": dict(fn=sig_bos, space={"k": [2, 3, 5], "window": [6, 12, 24], "tol_atr": [0.0, 0.3, 0.6]}, requires=[],
        text="After a break of structure, the first pullback to the broken level offers continuation entries.",
        why="Broken resistance flips to support as trapped counter-trend traders exit (SMC BOS/CHoCH).",
        falsify="Post-BOS pullbacks do not continue more often than not."),
    "funding_contrarian": dict(fn=sig_funding, space={"w": [250, 500, 1000], "pct": [0.03, 0.05, 0.1]}, requires=["funding"],
        text="Extreme funding signals crowded positioning; crowded side is vulnerable to squeezes.",
        why="Leverage crowding raises liquidation-cascade risk toward the crowded side's losers.",
        falsify="Extreme funding percentiles do not precede adverse returns for the crowded side."),
    "taker_flow": dict(fn=sig_taker, space={"n": [1, 3, 6], "w": [100, 250], "k": [1.5, 2.0, 2.5], "mode": ["divergence", "continuation"]}, requires=["taker"],
        text="Aggressive taker imbalance (CVD proxy) either continues or diverges from price in predictable ways.",
        why="Order-flow imbalance has price impact; divergence indicates passive absorption.",
        falsify="Imbalance z-score has no incremental predictive power for forward returns."),
    "volume_absorption": dict(fn=sig_absorb, space={"vz": [1.5, 2.0, 3.0], "rng_atr": [0.8, 1.2], "imb": [0.05, 0.1, 0.2]}, requires=["taker"],
        text="High volume with compressed range and one-sided aggression is absorbed by passive liquidity; the aggressor loses.",
        why="Passive limit orders absorbing market orders precede reversals (volume absorption).",
        falsify="Absorption bars show no reversal drift."),
    "session_drift": dict(fn=sig_session, space={"hours": None, "dir": [1, -1]}, requires=[],
        text="Specific UTC hours show persistent drift due to participant schedules (e.g., US/Asia opens).",
        why="Time-of-day flows (ETF/futures opens, funding settlement) are structural. Hours are FITTED ON TRAIN ONLY.",
        falsify="Fitted hour drift sign is not reproduced on validation data."),
    "vol_regime_trend": dict(fn=sig_volreg, space={"w": [250, 500], "lo": [0.3, 0.5, 0.7], "n": [50, 100], "mode": ["high", "low"]}, requires=[],
        text="Trend-following edge is concentrated in specific volatility regimes.",
        why="Regime-dependence of momentum: trends need volatility expansion to pay costs.",
        falsify="Regime-filtered trend does not beat unfiltered trend OOS."),
    "vwap_reversion": dict(fn=sig_vwap, space={"k": [1.0, 1.5, 2.0, 3.0], "min_hour": [4, 8, 12]}, requires=[],
        text="Price stretched far from the intraday (UTC-session) VWAP reverts toward it.",
        why="VWAP is the benchmark for institutional execution; deviations attract mean-reverting algorithmic flow.",
        falsify="Stretch-from-VWAP bars show no reversion drift after costs."),
    "narrow_range_breakout": dict(fn=sig_nr, space={"n": [4, 7, 10]}, requires=[],
        text="The narrowest bar of a recent window precedes directional expansion (Crabel NR4/NR7).",
        why="Range contraction signals inventory balance; resolution is directional and volatility clusters.",
        falsify="Break of an NR bar is no more directional than any other bar."),
    "eth_btc_ratio_reversion": dict(fn=sig_ratio, space={"n": [100, 250, 500], "k": [1.5, 2.0, 2.5]}, requires=["ref"],
        text="The ETH/BTC log-ratio mean-reverts; trade the relatively cheap asset long and the rich asset short.",
        why="Cross-asset relative value: shared beta factor, idiosyncratic flows are transient.",
        falsify="Extreme ratio z-scores are not followed by convergence."),
    "gp_rule": dict(fn=sig_gp, space={"rule": None, "side": None}, requires=[],
        text="Interpretable conjunctions of causal market-state conditions discovered by genetic programming over a constrained grammar.",
        why="Edges are more likely to live in conditional states (momentum x regime x flow x session) than in single indicators; GP searches this space with diversity control.",
        falsify="Rule fails the identical out-of-sample, cost-stress, placebo and multiple-testing gates as every other family."),
    "failed_breakout_fade": dict(fn=sig_failbreak, space={"n": [20, 40, 80], "k": [1, 2, 4]}, requires=[],
        text="Breakouts that close back inside the range within a few bars trap breakout traders; the squeeze of their stops drives a reversal.",
        why="Trapped-trader dynamics: stops of late breakout buyers/sellers fuel the move back (momentum exhaustion).",
        falsify="Bars after a failed breakout show no reversal drift versus matched random bars."),
    "cross_asset_leadlag": dict(fn=sig_leadlag, space={"n": [1, 2, 4], "thr_atr": [0.5, 1.0, 1.5]}, requires=["ref"],
        text="BTC leads ETH (and vice versa) at short horizons; laggard catches up.",
        why="Information is priced first in the most liquid instrument; arbitrageurs equilibrate with delay.",
        falsify="Reference return does not predict laggard's next-bar return."),
}

# families that need data we do NOT have (never faked): recorded as blocked-by-data hypotheses
BLOCKED_HYPOTHESES = {
    "open_interest_divergence": "Needs historical open-interest history (not in loaded dataset).",
    "liquidation_cascade_fade": "Needs liquidation event feed (not available).",
    "orderbook_imbalance": "Needs historical L2 order-book snapshots (not available).",
    "bid_ask_spread_regime": "Needs historical bid/ask quotes (not available).",
}

FILTER_SPACE = {  # optional AND-filters appended by Evolution agent
    "ema_trend": {"n": [50, 100, 200]},
    "atr_pct": {"w": [500], "lo": [0.2, 0.4], "hi": [0.8, 1.0]},
    "session": {"hours": [list(range(0, 8)), list(range(8, 16)), list(range(16, 24))]},
    "htf_trend": {"mult": [4, 12, 16, 48], "n": [20, 50]},
    "er_regime": {"n": [20, 50], "mode": ["trend", "range"], "thr": [0.2, 0.3]},
}


def canonical(spec: dict) -> dict:
    keep = ("family", "params", "side", "stop_atr", "target_rr", "max_hold", "atr_n", "filters")
    d = {k: spec.get(k) for k in keep}
    d["filters"] = spec.get("filters") or []
    return d


def entry_key(spec: dict) -> str:
    """Identity of the ENTRY signal (family, params, filters, side) - independent of exits and sizing."""
    return hashlib.sha1(json.dumps([spec.get("family"), spec.get("params"), spec.get("filters") or [], spec.get("side")], sort_keys=True, default=list).encode()).hexdigest()[:16]


def spec_hash(spec: dict) -> str:
    return hashlib.sha1(json.dumps(canonical(spec), sort_keys=True, default=list).encode()).hexdigest()[:16]


def validate_spec(spec: dict) -> list[str]:
    """Returns list of problems (empty = valid). Used on AI-proposed specs (never trusted blindly)."""
    errs = []
    fam = FAMILIES.get(spec.get("family"))
    if not fam: return [f"unknown family {spec.get('family')}"]
    p = spec.get("params") or {}
    if spec.get("family") == "gp_rule":
        from . import grammar
        errs = grammar.validate_rule(p.get("rule"), p.get("side")) + ([] if spec.get("side") == p.get("side") else ["side mismatch"])
        if spec.get("stop_atr") is None or spec["stop_atr"] <= 0 or spec.get("max_hold", 0) <= 0: errs.append("bad exits")
        return errs
    for k, grid in fam["space"].items():
        if k not in p: errs.append(f"missing param {k}")
        elif grid is not None and p[k] not in grid and not (isinstance(p[k], (int, float)) and grid and all(isinstance(g, (int, float)) for g in grid) and min(grid) <= p[k] <= max(grid)):
            errs.append(f"param {k}={p[k]} out of range")
    for k in ("stop_atr", "max_hold"):
        if spec.get(k) is None or spec[k] <= 0: errs.append(f"bad {k}")
    if spec.get("side") not in SIDES: errs.append("bad side")
    if spec.get("target_rr", 0) < 0: errs.append("bad target_rr")
    for f in spec.get("filters") or []:       # filters are data, never code: strict whitelist + ranges
        t = f.get("type") if isinstance(f, dict) else None
        if t not in FILTER_SPACE: errs.append(f"bad filter {t}"); continue
        if t == "ema_trend" and not (isinstance(f.get("n"), int) and 5 <= f["n"] <= 500): errs.append("bad ema n")
        if t == "atr_pct" and not (all(isinstance(f.get(k), (int, float)) for k in ("lo", "hi")) and 0 <= f["lo"] < f["hi"] <= 1.0): errs.append("bad atr_pct range")
        if t == "htf_trend" and not (f.get("mult") in (2, 3, 4, 6, 8, 12, 16, 48, 96, 288) and isinstance(f.get("n"), int) and 5 <= f["n"] <= 200): errs.append("bad htf_trend")
        if t == "er_regime" and not (isinstance(f.get("n"), int) and 5 <= f["n"] <= 200 and f.get("mode") in ("trend", "range") and isinstance(f.get("thr"), (int, float)) and 0 < f["thr"] < 1): errs.append("bad er_regime")
        if t == "session" and not (isinstance(f.get("hours"), list) and f["hours"] and all(isinstance(h, int) and 0 <= h < 24 for h in f["hours"])): errs.append("bad session hours")
    if len(spec.get("filters") or []) > 3: errs.append("too many filters")
    if spec.get("stop_atr", 0) and spec["stop_atr"] > 10: errs.append("stop too wide")
    return errs


def apply_filters(F, spec, L, S):
    for f in spec.get("filters") or []:
        t = f["type"]
        if t == "ema_trend":
            e = F.ema(f["n"]); L = L & (F.c > e); S = S & (F.c < e)
        elif t == "atr_pct":
            pr = F.atr_pct(14, f.get("w", 500)); m = (pr >= f["lo"]) & (pr <= f["hi"]); L = L & m; S = S & m
        elif t == "session":
            m = np.isin(F.hour, f["hours"]); L = L & m; S = S & m
        elif t == "htf_trend":
            e = F.htf_ema(f["mult"], f["n"]); L = L & (F.c > e); S = S & (F.c < e)
        elif t == "er_regime":
            er = F.efficiency_ratio(f["n"]); m = (er >= f["thr"]) if f["mode"] == "trend" else (er < f["thr"]); m = np.nan_to_num(m.astype(float)) > 0; L = L & m; S = S & m
    return L, S


def signals(F, spec):
    L, S = FAMILIES[spec["family"]]["fn"](F, spec["params"])
    L, S = apply_filters(F, spec, np.asarray(L, bool), np.asarray(S, bool))
    side = spec.get("side", "both")
    if side == "long": S = np.zeros_like(S)
    if side == "short": L = np.zeros_like(L)
    warm = 600
    L[:warm] = False; S[:warm] = False
    return L, S


def neighbors(spec, max_n=12, rng=None):
    """Parameter-sensitivity neighbours: step each tunable param to adjacent grid values."""
    fam = FAMILIES[spec["family"]]; out = []
    def step(space, cur, key, setter):
        grid = space.get(key)
        if not grid or cur not in grid: return
        i = grid.index(cur)
        for j in (i - 1, i + 1):
            if 0 <= j < len(grid): out.append(setter(grid[j]))
    import copy
    for k in fam["space"]:
        def setp(v, k=k):
            s = copy.deepcopy(spec); s["params"][k] = v; return s
        if fam["space"][k] is not None: step(fam["space"], spec["params"].get(k), k, setp)
    for k in ("stop_atr", "target_rr", "max_hold"):
        def sete(v, k=k):
            s = copy.deepcopy(spec); s[k] = v; return s
        step(EXIT_SPACE, spec.get(k), k, sete)
    return out[:max_n] if rng is None else [out[i] for i in rng.permutation(len(out))[:max_n]]
