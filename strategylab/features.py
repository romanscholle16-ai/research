"""Causal feature store. Every feature at bar i uses data up to and including bar i CLOSE only.
Signals computed from these are executed at the OPEN of bar i+1 (see backtest)."""
from __future__ import annotations
import numpy as np, pandas as pd


class Features:
    def __init__(self, df: pd.DataFrame, ref: pd.DataFrame | None = None):
        self.df = df
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.v = df["volume"].to_numpy(float)
        self.n = len(df)
        self._c = {}
        self.ref = ref  # other-instrument frame at same timeframe (for cross-asset)
        # unit-independent epoch seconds (pandas 3 may use us/ms/ns resolution)
        self.epoch_s = ((df.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")
        self.hour = df.index.hour.to_numpy()
        self.minute = df.index.minute.to_numpy()
        self.dow = df.index.dayofweek.to_numpy()

    def _memo(self, key, fn):
        if key not in self._c: self._c[key] = fn()
        return self._c[key]

    def s(self, name):
        return pd.Series(getattr(self, name))

    def atr(self, n=14):
        def f():
            pc = np.r_[self.c[0], self.c[:-1]]
            tr = np.maximum(self.h - self.l, np.maximum(abs(self.h - pc), abs(self.l - pc)))
            return pd.Series(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()
        return self._memo(("atr", n), f)

    def sma(self, n): return self._memo(("sma", n), lambda: self.s("c").rolling(n).mean().to_numpy())
    def ema(self, n): return self._memo(("ema", n), lambda: self.s("c").ewm(span=n, adjust=False, min_periods=n).mean().to_numpy())
    def std(self, n): return self._memo(("std", n), lambda: self.s("c").rolling(n).std().to_numpy())

    def zscore(self, n):
        return (self.c - self.sma(n)) / self.std(n)

    def rsi(self, n=14):
        def f():
            d = self.s("c").diff()
            up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
            dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
            return (100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy()
        return self._memo(("rsi", n), f)

    def hh(self, n):  # highest high of PREVIOUS n bars (excludes current)
        return self._memo(("hh", n), lambda: self.s("h").rolling(n).max().shift(1).to_numpy())

    def ll(self, n):
        return self._memo(("ll", n), lambda: self.s("l").rolling(n).min().shift(1).to_numpy())

    def ret(self, n):
        return self._memo(("ret", n), lambda: (self.s("c") / self.s("c").shift(n) - 1).to_numpy())

    def bandwidth(self, n=20):
        return self._memo(("bw", n), lambda: (4 * self.std(n) / self.sma(n)))

    def pct_rank(self, arr_key, arr, n):
        """Rolling percentile rank of the last value within its trailing window (fraction of PREVIOUS values below it). Causal, vectorised."""
        def f():
            r = pd.Series(arr).rolling(n, min_periods=n // 2)
            rank = r.rank(method="min"); cnt = r.count()
            return ((rank - 1) / (cnt - 1).replace(0, np.nan)).to_numpy()
        return self._memo(("pr", arr_key, n), f)

    def atr_pct(self, n=14, w=500):
        return self.pct_rank(("atr", n), self.atr(n) / self.c, w)

    def taker_imb(self, n=1):
        """(buy - sell)/total taker volume over n bars, in [-1,1]. CVD proxy from kline taker-buy volume."""
        def f():
            tb = self.df["taker_buy_volume"].rolling(n).sum(); tv = self.df["volume"].rolling(n).sum()
            return ((2 * tb - tv) / tv.replace(0, np.nan)).to_numpy()
        return self._memo(("imb", n), f)

    def vol_z(self, n=50):
        return self._memo(("vz", n), lambda: ((self.s("v") - self.s("v").rolling(n).mean()) / self.s("v").rolling(n).std()).to_numpy())

    def funding(self): return self._memo("fund", lambda: self.df["funding"].to_numpy())

    def fund_pct(self, w=500):
        return self.pct_rank("fund", self.funding(), w)

    def swing_high(self, k=3):
        """Confirmed pivot highs (needs k bars to the right => known only k bars later). Returns last confirmed level, causal."""
        def f():
            h = self.s("h"); piv = (h == h.rolling(2 * k + 1, center=True).max())
            lvl = h.where(piv).shift(k)  # known at bar i+k
            return lvl.ffill().to_numpy()
        return self._memo(("swh", k), f)

    def swing_low(self, k=3):
        def f():
            l = self.s("l"); piv = (l == l.rolling(2 * k + 1, center=True).min())
            return l.where(piv).shift(k).ffill().to_numpy()
        return self._memo(("swl", k), f)

    def ref_ret(self, n):
        def f():
            if self.ref is None: return np.full(self.n, np.nan)
            rc = self.ref["close"].reindex(self.df.index)
            return (rc / rc.shift(n) - 1).to_numpy()
        return self._memo(("rref", n), f)

    def vwap_dev(self):
        """(close - session VWAP)/ATR, session = UTC day, cumulative up to and including this bar (causal)."""
        def f():
            day = self.epoch_s // 86400
            qv = pd.Series(self.df["quote_volume"].to_numpy()).groupby(day).cumsum()
            v = pd.Series(self.v).groupby(day).cumsum()
            vw = (qv / v.replace(0, np.nan)).to_numpy()
            return (self.c - vw) / self.atr(14)
        return self._memo("vwapdev", f)

    def ratio_z(self, n):
        """z-score of own/reference price ratio (cross-asset relative value)."""
        def f():
            if self.ref is None: return np.full(self.n, np.nan)
            rc = self.ref["close"].reindex(self.df.index).to_numpy()
            r = pd.Series(np.log(self.c / rc))
            return ((r - r.rolling(n).mean()) / r.rolling(n).std()).to_numpy()
        return self._memo(("ratioz", n), f)

    def htf_ema(self, mult, n):
        """EMA of the close of COMPLETED higher-timeframe bars (mult x current bar), forward-filled; never uses the forming HTF bar."""
        def f():
            step = int(np.median(np.diff(self.epoch_s[:2000]))) * mult
            g = pd.Series(self.c).groupby(self.epoch_s // step)
            last = g.last(); ema = last.ewm(span=n, adjust=False, min_periods=n).mean().shift(1)   # shift: only completed HTF bars
            return ema.reindex(self.epoch_s // step).to_numpy()
        return self._memo(("htf", mult, n), f)

    def efficiency_ratio(self, n):
        """Kaufman efficiency ratio in [0,1]: |net move| / sum|moves|. High = trending, low = choppy."""
        def f():
            c = self.s("c"); net = (c - c.shift(n)).abs(); path = c.diff().abs().rolling(n).sum()
            return (net / path.replace(0, np.nan)).to_numpy()
        return self._memo(("er", n), f)

    def taker_imb_z(self, n=1, w=250):
        """z-score of the n-bar taker imbalance against its trailing w-bar mean/std (CVD proxy)."""
        def f():
            imb = pd.Series(self.taker_imb(n)); r = imb.rolling(w, min_periods=w // 2)
            return ((imb - r.mean()) / r.std()).to_numpy()
        return self._memo(("imbz", n, w), f)

    def atr_frac_med(self, lo, hi, n=14):
        """Median ATR/price over [lo,hi): cheap per-timeframe volatility level (needs no signals)."""
        def f():
            a = self.atr(n)[lo:hi] / self.c[lo:hi]; a = a[np.isfinite(a)]
            return float(np.median(a)) if len(a) else float("nan")
        return self._memo(("atrfm", lo, hi, n), f)
