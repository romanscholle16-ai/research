"""Strategy families, parameter grids, canonical identities and causal signal builders.

A *spec* is a plain dict: {family, symbol, timeframe, params, account}. Equivalent specs normalise to the same
canonical id so the registry never repeats an experiment. All signal builders are causal: the signal at index i
uses only bars <= i (verified by Guardian 3's truncation probe and by tests)."""
import hashlib, json
import numpy as np

from . import config, indicators as I
from .engine import Signals

RISK_GRID = {"stop_atr": [1.0, 1.5, 2.0, 3.0], "tp_r": [0.0, 1.5, 3.0], "max_hold": [12, 24, 48, 96]}

F = {}          # family -> dict(agent, grid, fn, other, info)


def family(name, agent, grid, other=False, risk=True, title="", hypothesis="", rationale="", falsifier=""):
    def deco(fn):
        g = dict(grid)
        if risk:
            for k, v in RISK_GRID.items():
                g.setdefault(k, v)
        F[name] = dict(agent=agent, grid=g, fn=fn, other=other, risk=risk,
                       info=dict(title=title or name, hypothesis=hypothesis, rationale=rationale, falsifier=falsifier))
        return fn
    return deco


def _fresh(cond):
    """True only on the bar where cond becomes true (avoids re-entering on every bar of a persistent state)."""
    prev = np.concatenate(([False], cond[:-1]))
    return cond & ~prev


def _build(m, p, lg, sh, xl=None, xs=None):
    lg = np.asarray(lg, bool); sh = np.asarray(sh, bool)
    side = np.where(lg & ~sh, 1, np.where(sh & ~lg, -1, 0)).astype(np.int8)
    a = I.atr(m, 14)
    sd = p.get("stop_atr", 2.0) * a
    return Signals(side=side, stop_dist=sd, tp_r=float(p.get("tp_r", 0.0)), max_hold=int(p.get("max_hold", 48)),
                   exit_long=xl, exit_short=xs)


# ================================================================ Agent 1: market structure ====
@family("swing_break", "market_structure", {"swing_k": [2, 3, 5, 8], "trend_filter": ["none", "ema50", "ema200"]},
        title="Confirmed swing-high/low break", hypothesis="Closes through the last confirmed swing extreme continue in the break direction.",
        rationale="Breaks of structure mark liquidity being cleared and a possible regime change.",
        falsifier="Expectancy <= 0 after costs across swing sizes means breaks do not carry information at this horizon.")
def swing_break(m, p):
    sh_, sl_ = I.pivots(m, p["swing_k"])
    ph, pl = I.shift(sh_, 1), I.shift(sl_, 1)
    with np.errstate(invalid="ignore"):
        lg = _fresh(m.c > ph); sh = _fresh(m.c < pl)
        if p["trend_filter"] != "none":
            e = I.c_ema(m, int(p["trend_filter"][3:]))
            lg &= m.c > e; sh &= m.c < e
    return _build(m, p, lg, sh)


@family("liq_sweep", "market_structure", {"n": [10, 20, 40, 80], "wick": [0.3, 0.5]},
        title="Liquidity sweep and reclaim", hypothesis="A wick through the n-bar extreme that closes back inside reverses.",
        rationale="Stops resting beyond extremes get swept; once absorbed, price tends to return.",
        falsifier="No positive net expectancy for sweeps of any lookback.")
def liq_sweep(m, p):
    n = p["n"]
    pmin, pmax = I.shift(I.roll_min(m.l, n), 1), I.shift(I.roll_max(m.h, n), 1)
    rng = np.maximum(m.h - m.l, 1e-9)
    lw, uw = (np.minimum(m.o, m.c) - m.l) / rng, (m.h - np.maximum(m.o, m.c)) / rng
    with np.errstate(invalid="ignore"):
        lg = (m.l < pmin) & (m.c > pmin) & (lw >= p["wick"])
        sh = (m.h > pmax) & (m.c < pmax) & (uw >= p["wick"])
    return _build(m, p, lg, sh)


@family("struct_pullback", "market_structure", {"reg_n": [100, 200], "pb_n": [20, 50], "slope_n": [24, 72]},
        title="Pullback inside a structural trend", hypothesis="In an established trend, tags of the pullback EMA resume the trend.",
        rationale="Trend-state conditioning may improve continuation odds versus unconditional breakouts.",
        falsifier="Pullback entries lose to unconditional trend entries on the same data.")
def struct_pullback(m, p):
    reg, pb = I.c_ema(m, p["reg_n"]), I.c_ema(m, p["pb_n"])
    slope = reg - I.shift(reg, p["slope_n"])
    with np.errstate(invalid="ignore"):
        up, dn = (m.c > reg) & (slope > 0), (m.c < reg) & (slope < 0)
        lg = _fresh(up & (m.l <= pb) & (m.c > pb)); sh = _fresh(dn & (m.h >= pb) & (m.c < pb))
    return _build(m, p, lg, sh)


# ================================================================== Agent 2: momentum/trend ====
@family("donchian", "momentum_trend", {"n": [20, 40, 80, 160], "exit_frac": [0.0, 0.5]},
        title="Donchian channel breakout", hypothesis="Closes beyond the n-bar channel persist (time-series momentum).",
        rationale="Trend-following has documented persistence in crypto at multi-day horizons.",
        falsifier="Breakouts show non-positive expectancy after costs for all n.")
def donchian(m, p):
    n = p["n"]
    up, dn = I.shift(I.roll_max(m.h, n), 1), I.shift(I.roll_min(m.l, n), 1)
    with np.errstate(invalid="ignore"):
        lg, sh = _fresh(m.c > up), _fresh(m.c < dn)
        xl = xs = None
        if p["exit_frac"] > 0:
            k = max(2, int(n * p["exit_frac"]))
            xl = m.c < I.shift(I.roll_min(m.l, k), 1)
            xs = m.c > I.shift(I.roll_max(m.h, k), 1)
    return _build(m, p, lg, sh, xl, xs)


@family("ema_cross", "momentum_trend", {"fast": [8, 12, 20, 30], "slow": [50, 80, 120, 200], "er_min": [0.0, 0.2, 0.3]},
        title="EMA crossover with efficiency filter", hypothesis="Fast/slow EMA crosses in efficient (trending) markets persist.",
        rationale="Efficiency-ratio gating removes chop where crossovers whipsaw.",
        falsifier="Gated crossovers are not better than ungated ones, and neither is positive.")
def ema_cross(m, p):
    f, s = I.c_ema(m, p["fast"]), I.c_ema(m, p["slow"])
    er = I.efficiency_ratio(m, 24)
    with np.errstate(invalid="ignore"):
        ok = (er >= p["er_min"]) if p["er_min"] > 0 else np.ones(len(m.c), bool)
        lg, sh = _fresh((f > s)) & ok, _fresh((f < s)) & ok
        xl, xs = (f < s), (f > s)
    return _build(m, p, lg, sh, xl, xs)


@family("tsmom", "momentum_trend", {"lb": [24, 48, 96, 168, 336], "thr": [0.5, 1.0, 1.5]},
        title="Time-series momentum threshold", hypothesis="Large lookback returns (in vol units) predict same-sign forward returns.",
        rationale="Volatility-normalised momentum is the standard TSMOM form.",
        falsifier="Forward returns after strong lookback moves are not same-signed after costs.")
def tsmom(m, p):
    lb = p["lb"]
    ret = np.log(m.c / I.shift(m.c, lb, m.c[0]))
    sd = I.roll_std(I.logret(m, 1), 168) * np.sqrt(lb)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = ret / sd
        lg, sh = _fresh(z > p["thr"]), _fresh(z < -p["thr"])
    return _build(m, p, lg, sh)


@family("er_breakout", "momentum_trend", {"er_n": [20, 40], "er_min": [0.3, 0.4, 0.5]},
        title="Efficiency-ratio trend onset", hypothesis="A jump in the efficiency ratio marks trend onset in the direction of net displacement.",
        rationale="Kaufman efficiency separates directional from noisy movement.", falsifier="No edge after costs for any threshold.")
def er_breakout(m, p):
    er = I.efficiency_ratio(m, p["er_n"])
    d = m.c - I.shift(m.c, p["er_n"], m.c[0])
    with np.errstate(invalid="ignore"):
        on = er > p["er_min"]
        lg, sh = _fresh(on & (d > 0)), _fresh(on & (d < 0))
    return _build(m, p, lg, sh)


# ===================================================================== Agent 3: mean reversion ==
@family("zscore_revert", "mean_reversion", {"n": [24, 48, 96, 168], "z": [1.5, 2.0, 2.5, 3.0], "regime": ["none", "lowvol", "ranging"],
                                             "exit_mid": [0, 1]},
        title="Volatility-normalised extreme reversion", hypothesis="Price far from its local mean (in sigmas) reverts, particularly in calm/ranging regimes.",
        rationale="Short-horizon overreaction and inventory effects produce mean reversion that trending regimes destroy.",
        falsifier="Extremes continue (negative expectancy) in every regime.")
def zscore_revert(m, p):
    z = I.zscore(m, p["n"])
    with np.errstate(invalid="ignore"):
        ok = np.ones(len(z), bool)
        if p["regime"] == "lowvol":
            ok = I.vol_pct(m) < 0.5
        elif p["regime"] == "ranging":
            ok = I.efficiency_ratio(m, 48) < 0.25
        lg, sh = _fresh(z < -p["z"]) & ok, _fresh(z > p["z"]) & ok
        xl = xs = None
        if p["exit_mid"]:
            xl, xs = z > 0, z < 0
    return _build(m, p, lg, sh, xl, xs)


@family("rsi_extreme", "mean_reversion", {"n": [7, 14, 21], "lo": [10, 15, 20, 30], "trend_gate": [0, 1]},
        title="RSI extremes", hypothesis="Oversold/overbought RSI readings revert; gating by the long-term trend improves it.",
        rationale="Classic oscillator reversion, tested with and without trend alignment.", falsifier="Neither gated nor ungated variants earn positive net expectancy.")
def rsi_extreme(m, p):
    r = I.rsi(m, p["n"])
    e = I.c_ema(m, 200)
    with np.errstate(invalid="ignore"):
        lg, sh = _fresh(r < p["lo"]), _fresh(r > 100 - p["lo"])
        if p["trend_gate"]:
            lg &= m.c > e; sh &= m.c < e
    return _build(m, p, lg, sh)


@family("bollinger_fade", "mean_reversion", {"n": [20, 50, 100], "k": [2.0, 2.5, 3.0], "confirm": [0, 1]},
        title="Bollinger band fade", hypothesis="Pierces of the outer band that close back inside revert toward the mean.",
        rationale="Confirmation bar reduces catching falling knives.", falsifier="Fades lose after costs; confirmation does not help.")
def bollinger_fade(m, p):
    mu, sd = I.c_mean(m, p["n"]), I.c_std(m, p["n"])
    up, dn = mu + p["k"] * sd, mu - p["k"] * sd
    with np.errstate(invalid="ignore"):
        if p["confirm"]:
            lg = (I.shift(m.c, 1) < I.shift(dn, 1)) & (m.c > dn)
            sh = (I.shift(m.c, 1) > I.shift(up, 1)) & (m.c < up)
        else:
            lg, sh = _fresh(m.c < dn), _fresh(m.c > up)
        xl, xs = m.c > mu, m.c < mu
    return _build(m, p, lg, sh, xl, xs)


# ============================================================ Agent 4: volume / flow proxies ====
@family("taker_imbalance", "volume_flow", {"n": [24, 72, 168], "thr": [1.0, 1.5, 2.0], "mode": ["momentum", "fade"], "persist": [1, 2, 3]},
        title="Taker-buy imbalance (candle proxy)", hypothesis="Persistent taker-buy dominance (buy-vol share z-score) predicts direction (or exhaustion if fade).",
        rationale="Aggressor-side volume is an order-flow PROXY from candle aggregates, not order-book data.",
        falsifier="Imbalance has no forward predictive value after costs in either mode.")
def taker_imbalance(m, p):
    share = m.tbv / np.where(m.v > 0, m.v, np.nan)
    z = (share - I.roll_mean(np.nan_to_num(share, nan=0.5), p["n"])) / np.where(I.roll_std(np.nan_to_num(share, nan=0.5), p["n"]) > 0, I.roll_std(np.nan_to_num(share, nan=0.5), p["n"]), np.nan)
    with np.errstate(invalid="ignore"):
        up, dn = z > p["thr"], z < -p["thr"]
        pu = np.ones(len(z), bool); pd_ = np.ones(len(z), bool)
        for k in range(p["persist"]):
            pu &= I.shift(up.astype(float), k, 0) > 0; pd_ &= I.shift(dn.astype(float), k, 0) > 0
        lg, sh = _fresh(pu), _fresh(pd_)
    if p["mode"] == "fade":
        lg, sh = sh, lg
    return _build(m, p, lg, sh)


@family("volume_spike", "volume_flow", {"n": [24, 72, 168], "thr": [2.0, 3.0, 4.0], "mode": ["continuation", "reversal"], "body_min": [0.0, 0.5]},
        title="Volume spike direction", hypothesis="Abnormal-volume candles carry information: continuation or reversal depending on regime.",
        rationale="Volume shocks reveal participation; direction of the candle indicates who was aggressive.", falsifier="Spikes carry no net edge in either mode.")
def volume_spike(m, p):
    lv = np.log1p(m.v)
    z = (lv - I.roll_mean(lv, p["n"])) / np.where(I.roll_std(lv, p["n"]) > 0, I.roll_std(lv, p["n"]), np.nan)
    body = np.abs(m.c - m.o) / np.maximum(m.h - m.l, 1e-9)
    with np.errstate(invalid="ignore"):
        spike = (z > p["thr"]) & (body >= p["body_min"])
        lg, sh = spike & (m.c > m.o), spike & (m.c < m.o)
    if p["mode"] == "reversal":
        lg, sh = sh, lg
    return _build(m, p, lg, sh)


@family("vwap_dev", "volume_flow", {"n": [24, 72, 168], "k": [1.5, 2.0, 3.0], "mode": ["revert", "trend"]},
        title="Rolling-VWAP deviation", hypothesis="Large deviations from rolling VWAP revert (or trend) depending on the mode.",
        rationale="VWAP approximates average participant cost basis.", falsifier="No mode survives costs.")
def vwap_dev(m, p):
    pv = m.c * m.v
    vw = I.roll_mean(pv, p["n"]) / np.where(I.roll_mean(m.v, p["n"]) > 0, I.roll_mean(m.v, p["n"]), np.nan)
    dev = m.c - vw
    sd = I.roll_std(np.nan_to_num(dev), p["n"])
    with np.errstate(invalid="ignore"):
        hi, lo = _fresh(dev > p["k"] * sd), _fresh(dev < -p["k"] * sd)
    lg, sh = (lo, hi) if p["mode"] == "revert" else (hi, lo)
    return _build(m, p, lg, sh)


# ================================================================== Agent 5: BTC-ETH relations ==
@family("ratio_z", "btc_eth", {"n": [48, 168, 336], "z": [1.5, 2.0, 2.5], "mode": ["revert", "trend"]}, other=True,
        title="ETH/BTC relative-value z-score", hypothesis="The primary asset's log-ratio to the other major mean-reverts (or trends).",
        rationale="Cross-asset relative value is a market-neutral-ish information source; trade the primary leg only here.",
        falsifier="Ratio extremes have no directional information for the primary leg after costs.")
def ratio_z(m, p):
    lr = np.log(m.c / m.xc)
    z = (lr - I.roll_mean(lr, p["n"])) / np.where(I.roll_std(lr, p["n"]) > 0, I.roll_std(lr, p["n"]), np.nan)
    with np.errstate(invalid="ignore"):
        hi, lo = _fresh(z > p["z"]), _fresh(z < -p["z"])
    lg, sh = (lo, hi) if p["mode"] == "revert" else (hi, lo)
    return _build(m, p, lg, sh)


@family("lead_lag", "btc_eth", {"k": [1, 2, 4], "thr": [1.0, 1.5, 2.0], "mode": ["follow", "fade"]}, other=True,
        title="Other-asset lead-lag", hypothesis="Large k-bar moves in the other major lead (or overshoot) the primary asset's next move.",
        rationale="Information diffuses across correlated perps with small lags; bars are timestamp-aligned and use closed bars only.",
        falsifier="The primary asset's next-bar return is independent of the other's recent move after costs.")
def lead_lag(m, p):
    k = p["k"]
    xr = np.log(m.xc / I.shift(m.xc, k, m.xc[0]))
    xsd = I.roll_std(np.log(m.xc / I.shift(m.xc, 1, m.xc[0])), 168) * np.sqrt(k)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = xr / xsd
        up, dn = _fresh(z > p["thr"]), _fresh(z < -p["thr"])
    lg, sh = (up, dn) if p["mode"] == "follow" else (dn, up)
    return _build(m, p, lg, sh)


@family("corr_break", "btc_eth", {"n": [48, 168], "corr_lo": [0.3, 0.5, 0.7], "z": [1.5, 2.0]}, other=True,
        title="Correlation-breakdown divergence", hypothesis="When BTC-ETH correlation drops, the stretched ratio snaps back.",
        rationale="Correlation breakdowns are transient; divergence trades are conditioned on them.", falsifier="No difference vs unconditional ratio reversion.")
def corr_break(m, p):
    a, b = I.logret(m, 1), np.log(m.xc / I.shift(m.xc, 1, m.xc[0]))
    n = p["n"]
    ma, mb = I.roll_mean(a, n), I.roll_mean(b, n)
    cov = I.roll_mean(a * b, n) - ma * mb
    corr = cov / np.where((I.roll_std(a, n) * I.roll_std(b, n)) > 0, (I.roll_std(a, n) * I.roll_std(b, n)) * (n - 1) / n, np.nan)
    lr = np.log(m.c / m.xc)
    z = (lr - I.roll_mean(lr, 168)) / np.where(I.roll_std(lr, 168) > 0, I.roll_std(lr, 168), np.nan)
    with np.errstate(invalid="ignore"):
        ok = corr < p["corr_lo"]
        lg, sh = _fresh(ok & (z < -p["z"])), _fresh(ok & (z > p["z"]))
    return _build(m, p, lg, sh)


# ================================================================ Agent 6: volatility regimes ===
@family("vol_expansion", "vol_regime", {"comp": [0.5, 0.6, 0.7], "n": [12, 24, 48], "win": [6, 12]},
        title="Compression-to-expansion breakout", hypothesis="After volatility compression, range breaks run further than average.",
        rationale="Volatility clusters and compresses before expanding.", falsifier="Post-compression breaks do not outperform unconditioned breaks.")
def vol_expansion(m, p):
    ratio = I.atr(m, 8) / I.atr(m, 48)
    comp = (I.roll_min(ratio, p["win"]) < p["comp"])
    up, dn = I.shift(I.roll_max(m.h, p["n"]), 1), I.shift(I.roll_min(m.l, p["n"]), 1)
    with np.errstate(invalid="ignore"):
        lg, sh = _fresh(comp & (m.c > up)), _fresh(comp & (m.c < dn))
    return _build(m, p, lg, sh)


@family("squeeze_break", "vol_regime", {"n": [20, 50], "pct": [0.1, 0.2, 0.3]},
        title="Bollinger-width squeeze", hypothesis="Band-width in its lowest percentile precedes directional moves.",
        rationale="Width percentile is a scale-free compression measure.", falsifier="Squeeze breaks lose after costs.")
def squeeze_break(m, p):
    n = p["n"]
    mu, sd = I.c_mean(m, n), I.c_std(m, n)
    width = sd / mu
    pr = I.pct_rank(np.nan_to_num(width, nan=1.0), 240)
    with np.errstate(invalid="ignore"):
        sq = pr <= p["pct"]
        was = I.roll_max(sq.astype(float), 6) > 0
        lg, sh = _fresh(was & (m.c > mu + 2 * sd)), _fresh(was & (m.c < mu - 2 * sd))
    return _build(m, p, lg, sh)


@family("vol_filtered_trend", "vol_regime", {"vmax": [0.3, 0.5, 0.7], "vmode": ["low", "high"], "fast": [12, 24], "slow": [72, 144]},
        title="Volatility-regime-filtered trend", hypothesis="Trend signals are only profitable in one volatility regime.",
        rationale="Regime conditioning of a plain signal isolates where it works.", falsifier="Filtering does not improve over the unfiltered signal.")
def vol_filtered_trend(m, p):
    f, s = I.c_ema(m, p["fast"]), I.c_ema(m, p["slow"])
    vp = I.vol_pct(m)
    with np.errstate(invalid="ignore"):
        ok = (vp <= p["vmax"]) if p["vmode"] == "low" else (vp >= 1 - p["vmax"])
        lg, sh = _fresh((f > s) & ok), _fresh((f < s) & ok)
        xl, xs = f < s, f > s
    return _build(m, p, lg, sh, xl, xs)


@family("highvol_revert", "vol_regime", {"vthr": [0.8, 0.9], "n": [48, 96], "z": [1.5, 2.0, 2.5]},
        title="High-volatility exhaustion", hypothesis="Extremes in high-vol regimes snap back faster.",
        rationale="Liquidation cascades overshoot and retrace.", falsifier="No retracement edge net of (higher) costs.")
def highvol_revert(m, p):
    vp, z = I.vol_pct(m), I.zscore(m, p["n"])
    with np.errstate(invalid="ignore"):
        ok = vp >= p["vthr"]
        lg, sh = _fresh(ok & (z < -p["z"])), _fresh(ok & (z > p["z"]))
    return _build(m, p, lg, sh)


# ====================================================================== Agent 7: sessions ======
@family("hour_bias", "sessions", {"hour": list(range(0, 24, 4)) + [1, 2, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23], "dir": [1, -1], "dow": ["all", "weekday", "weekend"]},
        title="Hour-of-day drift", hypothesis="Specific UTC hours show a persistent directional drift.",
        rationale="Liquidity/participation schedules (session opens, funding) could create stable intraday drift; stability across years is the test.",
        falsifier="Hour effects are not stable across walk-forward folds (likely noise).")
def hour_bias(m, p):
    hr = I.hour_of_day(m)
    prev_hr = (hr - m.tf_ms // 3_600_000) % 24      # decision bar precedes the entry bar
    dw = I.day_of_week(m)
    ok = np.ones(len(hr), bool) if p["dow"] == "all" else ((dw < 5) if p["dow"] == "weekday" else (dw >= 5))
    trig = (hr == (p["hour"] - m.tf_ms // 3_600_000) % 24) & ok
    lg, sh = (trig, np.zeros_like(trig)) if p["dir"] == 1 else (np.zeros_like(trig), trig)
    return _build(m, p, lg, sh)


@family("session_break", "sessions", {"start": [0, 7, 13], "rng": [1, 2, 4], "dow": ["all", "weekday"]},
        title="Opening-range breakout of a session", hypothesis="The first close beyond a session's opening range continues.",
        rationale="Session opens concentrate liquidity; early range is a reference for the session's direction.", falsifier="No continuation edge, or edge unstable by year.")
def session_break(m, p):
    n = len(m.c)
    hr, day = I.hour_of_day(m), m.t // 86_400_000
    dw = I.day_of_week(m)
    lg = np.zeros(n, bool); sh = np.zeros(n, bool)
    bars_per_h = max(1, m.tf_ms // 3_600_000)
    rb = max(1, p["rng"] // bars_per_h)
    starts = np.flatnonzero(hr == p["start"])
    for s0 in starts:
        e0 = s0 + rb
        if e0 >= n - 1 or (p["dow"] == "weekday" and dw[s0] >= 5) or day[e0] != day[s0]:
            continue
        hi_, lo_ = m.h[s0:e0].max(), m.l[s0:e0].min()
        end_day = np.searchsorted(day, day[s0], side="right")
        seg = slice(e0, min(end_day, e0 + 12))
        c = m.c[seg]
        up, dn = np.flatnonzero(c > hi_), np.flatnonzero(c < lo_)
        fu, fd = (up[0] if len(up) else 10**9), (dn[0] if len(dn) else 10**9)
        if fu < fd:
            lg[e0 + fu] = True
        elif fd < fu:
            sh[e0 + fd] = True
    return _build(m, p, lg, sh)


@family("dow_drift", "sessions", {"dow": [0, 1, 2, 3, 4, 5, 6], "dir": [1, -1]},
        title="Day-of-week drift", hypothesis="A weekday shows a persistent signed drift from the UTC day open.",
        rationale="Weekly seasonality (e.g. weekend liquidity) is commonly claimed; most such claims are noise.", falsifier="Effects flip sign between years.")
def dow_drift(m, p):
    hr, dw = I.hour_of_day(m), I.day_of_week(m)
    trig = (hr == 24 - m.tf_ms // 3_600_000) & (((dw + 1) % 7) == p["dow"])
    lg, sh = (trig, np.zeros_like(trig)) if p["dir"] == 1 else (np.zeros_like(trig), trig)
    return _build(m, p, lg, sh)


# ===================================================================== Agent 8: price action ====
@family("inside_bar", "price_action", {"filter": ["none", "ema50", "ema200"], "mode": ["break", "fade"], "min_range_atr": [0.0, 0.8]},
        title="Inside-bar breakout", hypothesis="After an inside bar, a close beyond the mother bar's range continues (or fails if fade).",
        rationale="Contraction within the prior range precedes expansion.", falsifier="No positive expectancy after costs in either mode.")
def inside_bar(m, p):
    ph, pl = I.shift(m.h, 1), I.shift(m.l, 1)
    pph, ppl = I.shift(m.h, 2), I.shift(m.l, 2)
    a = I.atr(m, 14)
    with np.errstate(invalid="ignore"):
        inside_prev = (ph <= pph) & (pl >= ppl) & ((pph - ppl) >= p["min_range_atr"] * I.shift(a, 1))
        up, dn = inside_prev & (m.c > pph), inside_prev & (m.c < ppl)
        if p["filter"] != "none":
            e = I.c_ema(m, int(p["filter"][3:]))
            up &= m.c > e; dn &= m.c < e
    lg, sh = (up, dn) if p["mode"] == "break" else (dn, up)
    return _build(m, p, lg, sh)


@family("pin_bar", "price_action", {"ratio": [0.6, 0.7], "n": [20, 50], "close_third": [0, 1]},
        title="Pin bar at range extreme", hypothesis="Long-wick rejection candles at n-bar extremes reverse.",
        rationale="Rejection at extremes signals absorbed aggression.", falsifier="Pin bars carry no information net of costs.")
def pin_bar(m, p):
    rng = np.maximum(m.h - m.l, 1e-9)
    lw, uw = (np.minimum(m.o, m.c) - m.l) / rng, (m.h - np.maximum(m.o, m.c)) / rng
    low_ext, high_ext = m.l <= I.roll_min(m.l, p["n"]), m.h >= I.roll_max(m.h, p["n"])
    with np.errstate(invalid="ignore"):
        lg, sh = (lw >= p["ratio"]) & low_ext, (uw >= p["ratio"]) & high_ext
        if p["close_third"]:
            lg &= (m.c - m.l) / rng > 0.66; sh &= (m.h - m.c) / rng > 0.66
    return _build(m, p, lg, sh)


@family("range_retest", "price_action", {"n": [24, 48, 96], "win": [3, 6, 12], "tol": [0.25, 0.5]},
        title="Breakout-and-retest", hypothesis="After a range break, a successful retest of the broken level gives a higher-quality entry.",
        rationale="Retests filter false breaks at the cost of missed trades.", falsifier="Retest entries do not beat direct breakout entries.")
def range_retest(m, p):
    n, win = p["n"], p["win"]
    up, dn = I.shift(I.roll_max(m.h, n), 1), I.shift(I.roll_min(m.l, n), 1)
    a = I.atr(m, 14)
    N = len(m.c)
    lg = np.zeros(N, bool); sh = np.zeros(N, bool)
    with np.errstate(invalid="ignore"):
        bu, bd = np.flatnonzero(_fresh(m.c > up)), np.flatnonzero(_fresh(m.c < dn))
    for b in bu:
        lvl, tol = up[b], p["tol"] * a[b]
        for k in range(b + 1, min(b + 1 + win, N)):
            if m.l[k] <= lvl + tol and m.c[k] > lvl:
                lg[k] = True; break
            if m.c[k] < lvl - tol:
                break
    for b in bd:
        lvl, tol = dn[b], p["tol"] * a[b]
        for k in range(b + 1, min(b + 1 + win, N)):
            if m.h[k] >= lvl - tol and m.c[k] < lvl:
                sh[k] = True; break
            if m.c[k] > lvl + tol:
                break
    return _build(m, p, lg, sh)


# ==================================================================== Agent 10: adversarial =====
@family("random_entry", "adversarial", {"seed": list(range(200)), "prob": [0.005, 0.01, 0.02]},
        title="Random-entry null model", hypothesis="Random entries with identical stop/target/hold rules earn zero edge before costs and negative after.",
        rationale="Null distribution that every promising rule must beat; quantifies how much 'edge' arises from the exit rules and drift alone.",
        falsifier="If random entries are persistently profitable, the exit rule or sample drift (not entry logic) explains results.")
def random_entry(m, p):
    g_hit, g_dir = (np.random.default_rng(x) for x in np.random.SeedSequence(int(p["seed"])).spawn(2))   # independent, prefix-stable streams
    n = len(m.c)
    hit = g_hit.random(n) < p["prob"]
    dirn = g_dir.random(n) < 0.5
    return _build(m, p, hit & dirn, hit & ~dirn)


@family("periodic_hold", "adversarial", {"period": [24, 72, 168], "dir": [1, -1], "offset": [0, 12]},
        title="Periodic long/short baseline", hypothesis="A naive periodic position earns the sample drift only.",
        rationale="Baseline to expose strategies that merely harvest ETH/BTC drift.", falsifier="Baseline beats sophisticated rules => sophistication adds nothing.")
def periodic_hold(m, p):
    idx = (np.arange(len(m.c)) % p["period"]) == p["offset"] % p["period"]
    z = np.zeros(len(idx), bool)
    return _build(m, p, idx if p["dir"] == 1 else z, z if p["dir"] == 1 else idx)


@family("inverse", "adversarial", {"base": [None]}, risk=False,
        title="Direction-inverted rule", hypothesis="If a rule's inverse is equally profitable the edge is structural noise (costs/drift), not direction.",
        rationale="Inversion is a sanity falsifier for any apparent predictive edge.", falsifier="Inverse performs materially worse than the base rule.")
def inverse(m, p):
    s = signals_for({**p["base"], "symbol": m.symbol, "timeframe": m.tf}, m)
    xl = s.exit_short; xs = s.exit_long
    return Signals(side=(-s.side).astype(np.int8), stop_dist=s.stop_dist, tp_r=s.tp_r, max_hold=s.max_hold, exit_long=xl, exit_short=xs)


# ======================================================================== Agent 9: hybrid =======
@family("hybrid", "hybrid", {"a": [None], "b": [None], "mode": ["filter", "and_window"], "win": [3, 6, 12]}, risk=False, other=True,
        title="Cross-family hybrid", hypothesis="Combining two individually supported signals from different families adds out-of-sample value.",
        rationale="Only components that each passed screening are combined; an ablation test requires the hybrid to beat both parts.",
        falsifier="Hybrid expectancy does not exceed the better component (ablation fails) => added complexity is overfit.")
def hybrid(m, p):
    sa = signals_for({**p["a"], "symbol": m.symbol, "timeframe": m.tf}, m)
    sb = signals_for({**p["b"], "symbol": m.symbol, "timeframe": m.tf}, m)
    w = p["win"]
    def recent(side, d):
        x = (side == d).astype(float)
        c = np.cumsum(np.insert(x, 0, 0))
        out = np.zeros(len(x), bool)
        out[w - 1:] = (c[w:] - c[:-w]) > 0
        return out
    lg = (sa.side == 1) & recent(sb.side, 1)
    sh = (sa.side == -1) & recent(sb.side, -1)
    if p["mode"] == "and_window":
        lg |= (sb.side == 1) & recent(sa.side, 1) & (sa.side != -1)
        sh |= (sb.side == -1) & recent(sa.side, -1) & (sa.side != 1)
    side = np.where(lg & ~sh, 1, np.where(sh & ~lg, -1, 0)).astype(np.int8)
    use = sa
    sd = np.where(np.isfinite(sa.stop_dist), sa.stop_dist, sb.stop_dist)
    return Signals(side=side, stop_dist=sd, tp_r=sa.tp_r, max_hold=sa.max_hold, exit_long=sa.exit_long, exit_short=sa.exit_short)


# ================================================================================ registry =======
def default_account():
    return {"risk_pct": config.RISK["risk_per_trade"], "leverage": config.RISK["default_leverage"]}


def normalize(spec: dict) -> dict:
    fam = spec["family"]
    if fam not in F:
        raise ValueError(f"unknown family {fam}")
    grid = F[fam]["grid"]
    p = {}
    for k, vals in grid.items():
        if fam in ("hybrid", "inverse") and k in ("a", "b", "base"):
            sub = spec["params"].get(k)
            if sub is None:
                raise ValueError(f"{fam} requires params.{k}")
            sub = normalize({**sub, "symbol": spec["symbol"], "timeframe": spec["timeframe"]})
            p[k] = {"family": sub["family"], "params": sub["params"]}
            continue
        v = spec["params"].get(k, vals[0])
        if v not in vals:
            raise ValueError(f"{fam}.{k}={v!r} not in grid")
        p[k] = v
    if fam == "ema_cross" and p["fast"] >= p["slow"]:
        raise ValueError("ema_cross requires fast < slow")
    tfh = {"1h": 1, "2h": 2, "4h": 4}[spec["timeframe"]]
    if fam in ("hour_bias",) and p["hour"] % tfh:
        raise ValueError("hour not aligned to timeframe")
    if fam == "hybrid":
        if p["a"]["family"] == p["b"]["family"] or F[p["a"]["family"]]["agent"] == F[p["b"]["family"]]["agent"]:
            raise ValueError("hybrid components must come from different families/agents")
        if p["a"]["family"] in ("hybrid", "inverse", "random_entry") or p["b"]["family"] in ("hybrid", "inverse", "random_entry"):
            raise ValueError("invalid hybrid component")
        if p["mode"] == "and_window" and json.dumps(p["a"], sort_keys=True) > json.dumps(p["b"], sort_keys=True):
            p["a"], p["b"] = p["b"], p["a"]    # symmetric mode: order is irrelevant -> canonical order
    acct = {**default_account(), **(spec.get("account") or {})}
    if F[fam]["other"] and spec["symbol"] not in config.DATA["symbols"]:
        raise ValueError("bad symbol")
    return {"family": fam, "symbol": spec["symbol"], "timeframe": spec["timeframe"], "params": p, "account": acct}


def canonical_id(spec: dict) -> str:
    return hashlib.sha256(json.dumps(normalize(spec), sort_keys=True).encode()).hexdigest()[:16]


def other_symbol(symbol):
    return "BTCUSDT" if symbol == "ETHUSDT" else "ETHUSDT"


def needs_other(spec) -> bool:
    fam = spec["family"]
    if fam == "hybrid":
        return True
    if fam == "inverse":
        return needs_other(spec["params"]["base"])
    return F[fam]["other"]


def signals_for(spec: dict, m) -> Signals:
    fam = spec["family"]
    p = dict(spec["params"])
    if fam in ("hybrid", "inverse"):
        return F[fam]["fn"](m, p)
    fnp = {k: p.get(k, v[0]) for k, v in F[fam]["grid"].items()}
    s = F[fam]["fn"](m, fnp)
    return s


def neighbors(spec: dict):
    """Adjacent grid points (one parameter moved one step) - used for parameter-sensitivity analysis and refinement."""
    spec = normalize(spec)
    fam, out = spec["family"], []
    if fam in ("hybrid", "inverse"):
        keys = ("a", "b") if fam == "hybrid" else ("base",)
        for k in keys:
            comp = {**spec["params"][k], "symbol": spec["symbol"], "timeframe": spec["timeframe"]}
            for nb in neighbors(comp):
                s2 = json.loads(json.dumps(spec))
                s2["params"][k] = {"family": nb["family"], "params": nb["params"]}
                try:
                    normalize(s2); out.append(s2)
                except ValueError:
                    pass
        return out
    for k, vals in F[fam]["grid"].items():
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals) or len(vals) < 2:
            continue
        sv = sorted(vals)
        i = sv.index(spec["params"][k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(sv):
                s2 = json.loads(json.dumps(spec))
                s2["params"][k] = sv[j]
                try:
                    normalize(s2)
                    out.append(s2)
                except ValueError:
                    pass
    return out


def grid_size(fam) -> int:
    n = 1
    for v in F[fam]["grid"].values():
        n *= len(v)
    return n


def make_spec(fam, symbol, tf, params):
    return {"family": fam, "symbol": symbol, "timeframe": tf, "params": params}
