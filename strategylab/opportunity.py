"""ETH opportunity-discovery engine.

Instead of enumerating indicator combinations, this module (1) labels EVERY historical bar with what a leveraged position opened there would have experienced
(path-dependent triple-barrier outcome, maximum favourable/adverse excursion, time to hit, with the same passive-fill, stop and cost model the backtester uses),
(2) measures how often 0.5-1.0% underlying moves occur, how often they are reachable without first breaching a liquidation-safe stop, and (3) searches for
recurring pre-conditions of favourable paths.

Leakage control (the whole point of the two-stage design):
 * DISCOVERY uses TRAIN bars only; feature thresholds (quantiles) are taken from train.  Labels look H bars ahead, so the last H(+fill) train bars are purged.
 * PREDICTION is measured on the VALIDATION period, starting H bars after the train/val boundary (embargo), on non-overlapping (spaced) signals only.
 * The locked test period is never read here; surviving patterns enter the normal funnel (family `opp_pattern`), which owns the one-shot locked test.
All features are computed from information available at the close of the decision bar."""
from __future__ import annotations
import json, math, time
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

FEATS = ["mom3", "mom6", "mom12", "mom24", "atr_ratio", "rng_exp", "body", "vz", "imbz", "ema50", "ema200", "vwap", "brk_up", "brk_dn", "swp_lo", "swp_hi",
         "wick_lo", "wick_hi", "hour4", "btc_lead", "btc_mom12", "fund_pct", "oi_z", "ls_top", "er20", "atr_pct"]
QS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)


def feature_matrix(F):
    """dict name -> float32 array (length n), causal at bar close."""
    def build():
        a = F.atr(14); c, o, h, l = F.c, F.o, F.h, F.l; rng = np.maximum(h - l, 1e-12); X = {}
        for n in (3, 6, 12, 24): X[f"mom{n}"] = F.ret(n) * c / a
        X["atr_ratio"] = F.atr(6) / F.atr(48); X["rng_exp"] = (h - l) / a; X["body"] = (c - o) / a
        X["vz"] = F.vol_z(50); X["imbz"] = F.taker_imb_z(3, 250)
        X["ema50"] = (c - F.ema(50)) / a; X["ema200"] = (c - F.ema(200)) / a; X["vwap"] = F.vwap_dev()
        hh, ll = F.hh(20), F.ll(20)
        X["brk_up"] = (c - hh) / a; X["brk_dn"] = (ll - c) / a
        X["swp_lo"] = np.where(c > ll, np.maximum(ll - l, 0), 0) / a; X["swp_hi"] = np.where(c < hh, np.maximum(h - hh, 0), 0) / a
        X["wick_lo"] = (np.minimum(o, c) - l) / rng; X["wick_hi"] = (h - np.maximum(o, c)) / rng
        X["hour4"] = (F.hour // 4).astype(float)
        X["btc_lead"] = (F.ref_ret(3) - F.ret(3)) * 100; X["btc_mom12"] = F.ref_ret(12) * 100
        X["fund_pct"] = F.fund_pct(500); X["oi_z"] = F.pos_z("oi", 12, 250); X["ls_top"] = F.pos_z("ls_top", 0, 250)
        X["er20"] = F.efficiency_ratio(20); X["atr_pct"] = F.atr_pct(14, 500)
        return {k: np.asarray(v, np.float32) for k, v in X.items()}
    return F._memo(("oppfeat",), build)


def _cost_fracs(em):
    """Round-trip cost (fraction of notional) by outcome, matching backtest.py."""
    ent = em.entry_cost()
    win = ent + em.exit_cost(stop=False)
    loss = ent + em.exit_cost(stop=True)
    tmo = ent + (em.taker_fee + em.half_spread + em.slippage) * em.cost_mult
    return win, loss, tmo


def label(F, side, s_atr, rr, H, em, lo, hi, limit=True, chunk=40000):
    """Path-dependent labels for decision bars t in [lo, hi-H-5).  Returns dict of arrays (index `t` = decision bar):
    ok (tradable & filled), outcome (+1 target, -1 stop, 0 timeout), R (net R-multiple after costs), mae, mfe (fractions of entry), tth (bars to resolution), stop_frac."""
    d = 1 if side == "long" else -1
    o, h, l, c = F.o, F.h, F.l, F.c; a = F.atr(14); pen = em.fill_pen; Lw = (em.limit_bars if limit and em.limit_bars else 0)
    t_all = np.arange(lo, max(lo, min(hi, F.n) - H - 5)); n = len(t_all)
    out = {k: np.full(n, np.nan) for k in ("R", "mae", "mfe", "tth", "stop_frac")}; out["outcome"] = np.zeros(n, np.int8); out["ok"] = np.zeros(n, bool); out["t"] = t_all
    cw, cl, ct = _cost_fracs(em)
    W = H + Lw + 1
    hw_all = sliding_window_view(h, W); lw_all = sliding_window_view(l, W)
    for s0 in range(0, n, chunk):
        t = t_all[s0:s0 + chunk]; m = len(t)
        hw = hw_all[t + 1]; lw = lw_all[t + 1]       # path bars t+1 .. t+W
        sd = s_atr * a[t]
        if Lw:
            lim = c[t]; thr = lim * (1 - pen) if d > 0 else lim * (1 + pen)
            touch = (lw[:, :Lw] <= thr[:, None]) if d > 0 else (hw[:, :Lw] >= thr[:, None])
            filled = touch.any(1); f = np.argmax(touch, 1)
            entry = np.where(d > 0, np.minimum(o[t + 1 + f], lim), np.maximum(o[t + 1 + f], lim))
        else:
            filled = np.ones(m, bool); f = np.zeros(m, int); entry = o[t + 1]
        ok = filled & np.isfinite(sd) & (sd > 0)
        stop_frac = sd / entry
        if getattr(em, "min_lev_capable", 0): ok &= em.tradable_stop(stop_frac)
        ok &= stop_frac >= 0.001                                        # backtester skips <0.1% stops
        k = np.arange(W)[None, :]; live = k >= f[:, None]                # bars at/after the fill bar
        stop_px = entry - d * sd; tgt_px = entry + d * rr * sd
        if d > 0: adv = (lw <= stop_px[:, None]) & live; fav = (hw >= tgt_px[:, None] * (1 + (pen if em.limit_exits else 0))) & live & (k > f[:, None])
        else: adv = (hw >= stop_px[:, None]) & live; fav = (lw <= tgt_px[:, None] * (1 - (pen if em.limit_exits else 0))) & live & (k > f[:, None])
        last = (H - 1) + f                                                  # time limit counted from the signal bar like the backtester (max_hold from signal)
        last = np.minimum(last, W - 1)
        beyond = k > last[:, None]; adv &= ~beyond; fav &= ~beyond
        big = W + 1
        ia = np.where(adv.any(1), np.argmax(adv, 1), big); iu = np.where(fav.any(1), np.argmax(fav, 1), big)
        win = iu < ia; loss = ia <= iu; loss &= ia < big
        tmo = ~win & ~loss
        end_i = np.where(win, iu, np.where(loss, ia, last))
        rows = np.arange(m)
        exit_px = np.where(win, tgt_px, np.where(loss, stop_px, c[t + 1 + end_i]))
        gross = d * (exit_px - entry) / entry
        cost = np.where(win, cw, np.where(loss, cl, ct))
        R = (gross - cost) / stop_frac
        # excursions up to resolution
        mask = (k >= f[:, None]) & (k <= end_i[:, None])
        if d > 0: mae = np.where(mask, entry[:, None] - lw, 0).max(1) / entry; mfe = np.where(mask, hw - entry[:, None], 0).max(1) / entry
        else: mae = np.where(mask, hw - entry[:, None], 0).max(1) / entry; mfe = np.where(mask, entry[:, None] - lw, 0).max(1) / entry
        sl = slice(s0, s0 + m)
        out["R"][sl] = R; out["mae"][sl] = mae; out["mfe"][sl] = mfe; out["tth"][sl] = end_i - f + 1; out["stop_frac"][sl] = stop_frac
        out["outcome"][sl] = np.where(win, 1, np.where(loss, -1, 0)); out["ok"][sl] = ok
    return out


def spaced(idx, gap):
    """Greedy non-overlapping selection of sorted decision indices (>= gap apart)."""
    keep, last = [], -10**12
    for i in idx:
        if i - last >= gap: keep.append(i); last = i
    return np.asarray(keep, int)


def _cond_mask(X, conds, n):
    m = np.ones(n, bool)
    for f, op, thr in conds:
        x = X[f]; m &= (x > thr) if op == ">" else (x <= thr)
        m &= np.isfinite(x)
    return m


def study(F, tf, em, lo, hi, H, s_frac=0.002, rrs=(2.0, 3.0, 4.0), limit=True):
    """Opportunity census on [lo,hi): frequency of favourable moves, reachability under a liquidation-safe stop, excursion distributions."""
    af = F.atr_frac_med(lo, hi, 14); s_atr = s_frac / af; out = dict(tf=tf, H=H, s_atr=round(float(s_atr), 3), atr_frac=round(float(af), 5), bars=int(hi - lo), configs=[])
    for side in ("long", "short"):
        for rr in rrs:
            lb = label(F, side, s_atr, rr, H, em, lo, hi, limit)
            ok = lb["ok"]; oc = lb["outcome"]
            if not ok.any(): continue
            n_ok = int(ok.sum()); wins = int((oc[ok] == 1).sum())
            # raw opportunity: price reaches the target within H ignoring the stop (path MFE >= target)
            raw = float(np.mean(lb["mfe"][ok] >= rr * s_frac * 0.999)) if False else None
            tgt_move = rr * s_frac
            w = ok & (oc == 1)
            out["configs"].append(dict(side=side, rr=rr, target_move_pct=round(100 * tgt_move, 3), stop_pct=round(100 * s_frac, 3), tradable_bars=n_ok, tradable_share=round(n_ok / len(ok), 3),
                base_win=round(wins / n_ok, 4), breakeven_win=round(float(-np.nanmean(np.where(oc[ok] == -1, lb["R"][ok], np.nan)) / (rr + 1)), 3) if False else None,
                mean_R_random=round(float(np.nanmean(lb["R"][ok])), 4), wins_per_week=round(float(wins) / max((hi - lo) * {"5m": 5, "15m": 15, "1h": 60}[tf] / 10080, 1e-9) / 1, 2) if False else None,
                mae_win_pct=[round(100 * float(x), 3) for x in np.nanpercentile(lb["mae"][w], [50, 90])] if w.any() else None,
                mfe_loss_pct=[round(100 * float(x), 3) for x in np.nanpercentile(lb["mfe"][ok & (oc == -1)], [50, 90])] if (ok & (oc == -1)).any() else None,
                tth_win_bars=[float(x) for x in np.nanpercentile(lb["tth"][w], [25, 50, 75])] if w.any() else None))
    return out


def discover(F, tf, em, bounds, side, s_frac, rr, H, max_depth=3, top_atoms=60, min_support=0.01, limit=True, rng=None):
    """TRAIN-only pattern search.  Returns (finalists, meta).  Each finalist: conds, train stats."""
    tlo, thi = bounds["train"]; X = feature_matrix(F)
    af = F.atr_frac_med(tlo, thi, 14); s_atr = s_frac / af
    lb = label(F, side, s_atr, rr, H, em, max(tlo, 600), thi - H - 6, limit)          # purge: last H+fill bars of train are not labelled
    t = lb["t"]; ok = lb["ok"]; R = lb["R"]; n = len(t)
    feats = {k: X[k][t] for k in FEATS}
    # atoms from TRAIN quantiles only
    atoms = []
    for k, x in feats.items():
        v = x[ok & np.isfinite(x)]
        if len(v) < 1000: continue
        for q in QS:
            thr = float(np.quantile(v, q))
            for op in (">", "<="): atoms.append((k, op, thr))
    base = float(np.nanmean(R[ok])) if ok.any() else 0.0
    Rv = np.where(ok, R, np.nan)
    def stat(mask):
        m = mask & ok
        c = int(m.sum())
        if c < min_support * max(ok.sum(), 1): return None
        return c, float(np.nanmean(Rv[m]))
    single = []
    for a_ in atoms:
        s = stat(_cond_mask(feats, [a_], n))
        if s: single.append((s[1], a_, s))
    single.sort(key=lambda x: -x[0]); top = [x[1] for x in single[:top_atoms]]
    cands = [([a_], s) for _, a_, s in single[:top_atoms]]
    tested = len(atoms)
    level = [[a_] for a_ in top]
    for depth in range(2, max_depth + 1):
        nxt = []
        for base_conds in level[:40 if depth > 2 else None]:
            bm = _cond_mask(feats, base_conds, n)
            used = {c[0] for c in base_conds}
            for a_ in (top if depth == 2 else [x[1] for x in single[:120]]):
                if a_[0] in used: continue
                tested += 1
                s = stat(bm & _cond_mask(feats, [a_], n))
                if s: nxt.append((s[1], base_conds + [a_], s))
        nxt.sort(key=lambda x: -x[0]); cands += [(c, s) for _, c, s in nxt[:300]]; level = [c for _, c, _ in nxt[:60]]
    # finalists: spaced (non-overlapping) train samples, t-stat of net R
    fin = []
    for conds, (cnt, mu) in sorted(cands, key=lambda x: -x[1][1])[:400]:
        m = _cond_mask(feats, conds, n) & ok; idx = spaced(np.flatnonzero(m), H)
        if len(idx) < 120: continue
        r = R[idx]; sd = r.std(ddof=1)
        if sd <= 0: continue
        tt = float(r.mean() / (sd / math.sqrt(len(idx))))
        fin.append(dict(conds=[[a, b, round(float(c), 5)] for a, b, c in conds], n_train=int(len(idx)), mean_R=float(r.mean()), t=tt, win=float(np.mean(lb["outcome"][idx] == 1)), signals=int(m.sum())))
    fin.sort(key=lambda x: -x["t"])
    uniq, seen = [], set()
    for f in fin:                               # diversity: one finalist per feature-set
        key = tuple(sorted({c[0] for c in f["conds"]}))
        if key in seen: continue
        seen.add(key); uniq.append(f)
    return uniq[:12], dict(side=side, s_frac=s_frac, rr=rr, H=H, s_atr=float(s_atr), base_R=base, tested=int(tested), train_ok=int(ok.sum()))


def predict(F, tf, em, bounds, finalist, meta, limit=True):
    """VALIDATION-period evaluation (embargoed by H bars from the train boundary), spaced signals, same labels."""
    vlo, vhi = bounds["val"]; H = meta["H"]; X = feature_matrix(F)
    lb = label(F, meta["side"], meta["s_atr"], meta["rr"], H, em, vlo + H + 6, vhi - H - 6, limit)
    t = lb["t"]; ok = lb["ok"]; R = lb["R"]; n = len(t)
    feats = {k: X[k][t] for k in FEATS}
    m = _cond_mask(feats, finalist["conds"], n) & ok; idx = spaced(np.flatnonzero(m), H)
    if len(idx) < 5: return dict(n_val=int(len(idx)), mean_R=None, t=None, win=None, base_R=float(np.nanmean(R[ok])) if ok.any() else None)
    r = R[idx]; sd = r.std(ddof=1); tt = float(r.mean() / (sd / math.sqrt(len(idx)))) if sd > 0 else 0.0
    return dict(n_val=int(len(idx)), mean_R=float(r.mean()), t=tt, win=float(np.mean(lb["outcome"][idx] == 1)), base_R=float(np.nanmean(R[ok])), base_win=float(np.mean(lb["outcome"][ok] == 1)),
                mae_win_pct=[round(100 * float(x), 3) for x in np.nanpercentile(lb["mae"][idx][lb["outcome"][idx] == 1], [50, 90])] if (lb["outcome"][idx] == 1).any() else None,
                trades_per_week=round(len(idx) / max((vhi - vlo - 2 * H - 12) * {"5m": 5, "15m": 15, "1h": 60, "1m": 1}[tf] / 10080, 1e-9), 2))


def to_spec(finalist, meta, atr_frac_med, max_hold=None):
    """Funnel strategy spec for a validated pattern (exits mirror the labelling barriers)."""
    side = meta["side"]
    return dict(family="opp_pattern", params=dict(conds=finalist["conds"], side=side), side=side, filters=[], stop_atr=round(float(meta["s_atr"]), 3), target_rr=float(meta["rr"]),
                max_hold=int(max_hold or meta["H"]), atr_n=14)


def signals(F, params):
    X = feature_matrix(F); n = F.n; side = params["side"]
    m = _cond_mask(X, [tuple(c) for c in params["conds"]], n)
    z = np.zeros(n, bool)
    return (m, z) if side == "long" else (z, m)
