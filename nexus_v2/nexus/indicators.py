"""Causal indicators: value at index i uses only data at indices <= i. Results cached per (market uid, key)."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

_C = {}


def _cached(m, key, fn):
    k = (m.uid, key)
    v = _C.get(k)
    if v is None:
        if len(_C) > 4000:
            _C.clear()
        v = _C[k] = fn()
    return v


def roll_mean(x, n):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def roll_std(x, n):
    x0 = x - np.nanmean(x[: max(n, 1000)]) if len(x) else x
    m1, m2 = roll_mean(x0, n), roll_mean(x0 * x0, n)
    return np.sqrt(np.maximum(m2 - m1 * m1, 0.0) * n / max(n - 1, 1))


def roll_max(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = swv(x, n).max(axis=1)
    return out


def roll_min(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = swv(x, n).min(axis=1)
    return out


def shift(x, k=1, fill=np.nan):
    out = np.full(len(x), fill, dtype=float)
    if k == 0:
        return np.asarray(x, float).copy()
    if k < len(x):
        out[k:] = x[:-k]
    return out


def ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty(len(x))
    acc = x[0]
    xs = x.tolist()
    o = out
    for i, v in enumerate(xs):
        acc = acc + a * (v - acc)
        o[i] = acc
    o[: n - 1] = np.nan
    return o


def atr(m, n=14):
    def f():
        pc = shift(m.c, 1, m.c[0])
        tr = np.maximum(m.h - m.l, np.maximum(np.abs(m.h - pc), np.abs(m.l - pc)))
        return roll_mean(tr, n)
    return _cached(m, ("atr", n), f)


def c_ema(m, n):
    return _cached(m, ("ema", n), lambda: ema(m.c, n))


def c_mean(m, n):
    return _cached(m, ("sma", n), lambda: roll_mean(m.c, n))


def c_std(m, n):
    return _cached(m, ("std", n), lambda: roll_std(m.c, n))


def zscore(m, n):
    return _cached(m, ("z", n), lambda: (m.c - roll_mean(m.c, n)) / np.where(roll_std(m.c, n) > 0, roll_std(m.c, n), np.nan))


def logret(m, k=1):
    return _cached(m, ("lr", k), lambda: np.log(m.c / shift(m.c, k, m.c[0])))


def efficiency_ratio(m, n):
    def f():
        net = np.abs(m.c - shift(m.c, n, m.c[0]))
        path = roll_mean(np.abs(m.c - shift(m.c, 1, m.c[0])), n) * n
        return net / np.where(path > 0, path, np.nan)
    return _cached(m, ("er", n), f)


def rsi(m, n):
    def f():
        d = m.c - shift(m.c, 1, m.c[0])
        up, dn = roll_mean(np.maximum(d, 0), n), roll_mean(np.maximum(-d, 0), n)
        return 100 - 100 / (1 + up / np.where(dn > 0, dn, np.nan))
    return _cached(m, ("rsi", n), f)


def pct_rank(x, n):
    """Trailing percentile rank of x[i] within x[i-n+1..i] (causal)."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = swv(x, n)
        out[n - 1:] = (w <= w[:, -1:]).mean(axis=1)
    return out


def vol_pct(m, short=24, long=240):
    return _cached(m, ("volpct", short, long), lambda: pct_rank(roll_std(logret(m, 1), short), long))


def hour_of_day(m):
    return ((m.t // 3_600_000) % 24).astype(int)


def day_of_week(m):
    return (((m.t // 86_400_000) + 3) % 7).astype(int)   # 0=Mon


def pivots(m, k):
    """Swing highs/lows confirmed k bars after the pivot bar: array value at i is the most recent CONFIRMED pivot
    (confirmation needs bars up to i), so there is no look-ahead."""
    def f():
        n = len(m.h)
        sh = np.full(n, np.nan); sl = np.full(n, np.nan)
        if n > 2 * k + 1:
            w_h, w_l = swv(m.h, 2 * k + 1), swv(m.l, 2 * k + 1)
            ph = w_h[:, k] >= w_h.max(axis=1)
            pl = w_l[:, k] <= w_l.min(axis=1)
            # window starting at s covers bars s..s+2k, pivot bar s+k, confirmed at bar s+2k
            idx = np.arange(len(ph)) + 2 * k
            sh[idx[ph]] = w_h[ph, k]
            sl[idx[pl]] = w_l[pl, k]
        def ffill(a):
            ok = ~np.isnan(a)
            i = np.where(ok, np.arange(n), 0)
            np.maximum.accumulate(i, out=i)
            r = a[i]
            r[: (np.argmax(ok) if ok.any() else n)] = np.nan
            return r
        return ffill(sh), ffill(sl)
    return _cached(m, ("piv", k), f)
