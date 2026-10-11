"""Deterministic bar-based backtester for single-position perpetual-futures strategies.

Execution rules (all conservative):
 * Signal evaluated at bar i close; entry = OPEN of bar i+1 with spread+slippage against us (market/taker).
 * Stops are stop-market: gap-through fills at the open; intrabar fills at stop minus stop slippage.
 * If stop and target are both inside one bar, the STOP is assumed to hit first.
 * Target fills only if high/low reaches target (no price improvement), charged taker fees.
 * Funding is paid/received at 00/08/16 UTC for each settlement the position is held through.
 * Trades open at range end are closed at last close ('end').
Outputs per-unit-notional returns so the capital engine can apply any sizing/leverage afterwards.
"""
from __future__ import annotations
import numpy as np
from .btcc import ExchangeModel, DEFAULT

REASONS = {0: "stop", 1: "target", 2: "time", 3: "end"}


class Trades:
    FIELDS = ("t_in", "t_out", "dir", "entry", "ret", "fee", "fund", "stop_frac", "mae", "adv_exit", "reason", "bars", "scale")

    def __init__(self, **kw):
        for f in self.FIELDS: setattr(self, f, np.asarray(kw.get(f, []), float))
        if len(self.scale) != len(self.t_in): self.scale = np.ones(len(self.t_in))   # risk scale (1 = standalone)

    def __len__(self): return len(self.t_in)

    @property
    def net(self): return self.ret - self.fee - self.fund          # per unit notional

    @property
    def R(self): return self.net / np.maximum(self.stop_frac, 1e-6)   # R-multiple (stop distance = 1R)

    def slice_time(self, t0, t1):
        m = (self.t_in >= t0) & (self.t_in < t1)
        return Trades(**{f: getattr(self, f)[m] for f in self.FIELDS})

    def to_json(self):
        return {f: np.round(getattr(self, f), 6).tolist() for f in self.FIELDS}

    @classmethod
    def from_json(cls, d): return cls(**d)

    @classmethod
    def concat(cls, ts):
        return cls(**{f: np.concatenate([getattr(t, f) for t in ts]) if ts else [] for f in cls.FIELDS})


def _fund_cum(F):
    ts_h, ts_m = F.hour, F.minute
    settle = (ts_h % 8 == 0) & (ts_m == 0)
    return np.cumsum(np.where(settle, F.funding(), 0.0))


def backtest(F, spec, L, S, em: ExchangeModel = DEFAULT, lo: int = 0, hi: int | None = None) -> Trades:
    n = F.n; hi = n if hi is None else min(hi, n)
    o, h, l, c = F.o, F.h, F.l, F.c
    atr = F.atr(spec.get("atr_n", 14)); fc = _fund_cum(F)
    ts = F.epoch_s
    stop_atr, rr, maxh = spec["stop_atr"], spec.get("target_rr", 0) or 0, int(spec["max_hold"])
    imp_in = (em.half_spread + em.slippage) * em.cost_mult
    imp_tgt = em.half_spread * em.cost_mult
    imp_stop = (em.half_spread + em.stop_slippage) * em.cost_mult
    fee = em.taker_fee * em.cost_mult; mfee = em.maker_fee * em.cost_mult
    hl = bool(em.min_lev_capable); lim_n = int(em.limit_bars); pen = em.fill_pen; lim_exits = bool(em.limit_exits)
    big = np.flatnonzero(np.diff(ts) > 12 * 3600) + 1      # first bar after a data outage (>12h): no position is held across it
    idx = np.flatnonzero((L | S))
    idx = idx[(idx >= lo) & (idx < hi - 2)]
    rows = []
    free = lo
    for i in idx:
        if i < free: continue
        if L[i] and S[i]: continue
        d = 1 if L[i] else -1
        a = atr[i]
        if not np.isfinite(a) or a <= 0: continue
        e = i + 1
        if hl and not em.tradable_stop(stop_atr * a / c[i]): continue      # high-leverage rule: stop must fit inside the liquidation distance and be affordable
        if lim_n:      # passive limit at the signal close; filled only when price trades through it within lim_n bars
            lim = c[i]; e0 = None
            for k in range(e, min(e + lim_n, hi - 1) + 1):
                if (d > 0 and l[k] <= lim * (1 - pen)) or (d < 0 and h[k] >= lim * (1 + pen)): e0 = k; break
            if e0 is None: continue
            entry = min(o[e0], lim) if d > 0 else max(o[e0], lim); e = e0
        else:
            entry = o[e] * (1 + d * imp_in)
        sd = stop_atr * a
        stop = entry - d * sd
        stop_frac = sd / entry
        if stop_frac < 0.001: continue
        tgt = entry + d * rr * sd if rr > 0 else None
        last = min(i + maxh, hi - 1)
        if len(big):
            g = big[np.searchsorted(big, e + 1)] if np.searchsorted(big, e + 1) < len(big) else None
            if g is not None and g <= last: last = g - 1
        if e > last: continue
        mae = 0.0; exit_px = None; reason = 2; j_exit = last
        for j in range(e, last + 1):
            if d > 0:
                if o[j] <= stop: exit_px, reason = o[j], 0
                elif l[j] <= stop: exit_px, reason = stop, 0
                else:
                    mae = max(mae, (entry - l[j]) / entry)
                    if tgt is not None and h[j] >= tgt * (1 + pen if lim_exits else 1) and not (lim_n and j == e): exit_px, reason = tgt, 1
            else:
                if o[j] >= stop: exit_px, reason = o[j], 0
                elif h[j] >= stop: exit_px, reason = stop, 0
                else:
                    mae = max(mae, (h[j] - entry) / entry)
                    if tgt is not None and l[j] <= tgt * (1 - pen if lim_exits else 1) and not (lim_n and j == e): exit_px, reason = tgt, 1
            if exit_px is not None: j_exit = j; break
        adv_exit = 0.0
        if exit_px is None:
            exit_px = c[last]; reason = 3 if last == hi - 1 and last < i + maxh else 2; j_exit = last
        imp = imp_stop if reason == 0 else (0.0 if reason == 1 and lim_exits else imp_tgt if reason == 1 else (em.half_spread + em.slippage) * em.cost_mult)
        fill = exit_px * (1 - d * imp)
        ret = d * (fill - entry) / entry
        if reason == 0: adv_exit = max(0.0, -ret)
        mae = max(mae, adv_exit)
        fund = d * (fc[j_exit] - fc[e])  # long pays positive funding
        rows.append((ts[e], ts[j_exit], d, entry, ret, (mfee if lim_n else fee) + (mfee if (reason == 1 and lim_exits) else fee), fund, stop_frac, mae, adv_exit, reason, j_exit - e + 1, 1.0))
        free = j_exit  # next signal may be evaluated at close of the exit bar
    if not rows: return Trades()
    arr = np.array(rows, float)
    return Trades(**{f: arr[:, k] for k, f in enumerate(Trades.FIELDS)})


def period_bounds(n, train_frac, val_frac):
    a = int(n * train_frac); b = int(n * (train_frac + val_frac))
    return {"train": (0, a), "val": (a, b), "test": (b, n)}


def metrics(tr: Trades, bars_per_year=None) -> dict:
    n = len(tr)
    if n == 0: return dict(n=0, mean_R=0.0, t=0.0, pf=0.0, win=0.0, net_mean=0.0, max_dd=0.0, total_R=0.0)
    R = tr.R; net = tr.net
    pos, neg = R[R > 0].sum(), -R[R < 0].sum()
    sd = R.std(ddof=1) if n > 1 else 0.0
    t = R.mean() / (sd / np.sqrt(n)) if sd > 0 else 0.0
    eq = np.cumprod(1 + np.clip(0.01 * R, -0.99, None))   # fixed 1%-risk compounding curve
    dd = float(np.max(1 - eq / np.maximum.accumulate(eq)))
    return dict(n=n, mean_R=float(R.mean()), t=float(t), pf=float(pos / neg) if neg > 0 else float("inf") if pos > 0 else 0.0,
                win=float((R > 0).mean()), net_mean=float(net.mean()), max_dd=dd, total_R=float(R.sum()),
                avg_stop=float(tr.stop_frac.mean()), avg_cost=float((tr.fee + tr.fund).mean()), avg_hold=float(tr.bars.mean()),
                stop_exit_frac=float((tr.reason == 0).mean()))


def weekly_coverage(tr: Trades, t0: float, t1: float) -> float:
    """Fraction of calendar weeks in [t0,t1) with at least one completed trade (exit time)."""
    wk = 7 * 86400
    w0 = int(t0 // wk); w1 = int((t1 - 1) // wk)
    if w1 < w0: return 0.0
    have = set((tr.t_out // wk).astype(int).tolist()) if len(tr) else set()
    return sum(1 for w in range(w0, w1 + 1) if w in have) / (w1 - w0 + 1)
