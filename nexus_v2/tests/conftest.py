import os, sys, tempfile, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="nexus_test_")
os.environ["NEXUS_HOME"] = _tmp           # tests never touch the production var/ directory
os.environ["NEXUS_NO_RELAUNCH"] = "1"     # unit tests must not let Guardian 1 spawn real supervisors
import pytest
import numpy as np


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def conn(home):
    from nexus import db
    c = db.open_db()
    yield c
    c.close()


def make_market(o, h, l, c, v=None, tf="1h", fund_t=(), fund_r=(), t0=1_700_000_000_000 // 3_600_000 * 3_600_000, xc=None):
    from nexus.data import Market, TF_MS
    n = len(o)
    ms = TF_MS[tf]
    return Market(symbol="TEST", tf=tf, tf_ms=ms, t=(t0 + np.arange(n) * ms).astype(np.int64), o=np.array(o, float),
                  h=np.array(h, float), l=np.array(l, float), c=np.array(c, float),
                  v=np.array(v if v is not None else [1000.0] * n, float), tbv=np.full(n, 500.0), xc=xc, xo=xc,
                  fund_t=np.array(fund_t, np.int64), fund_r=np.array(fund_r, float), version="testv", data_class="verified",
                  funding_known=len(fund_t) > 0, other=None)


def sig(n, at, side, stop_dist, tp_r=0.0, max_hold=10, exit_long=None, exit_short=None):
    from nexus.engine import Signals
    s = np.zeros(n, np.int8); s[at] = side
    return Signals(side=s, stop_dist=np.full(n, float(stop_dist)), tp_r=tp_r, max_hold=max_hold,
                   exit_long=exit_long, exit_short=exit_short)


def synth_arrays(n=40000, seed=7, regime_flip=1 / 150, drift=0.0010, sigma=0.0035, start=1500.0, t0=1_609_459_200_000):
    """Synthetic perpetual-like 1h bars with PERSISTENT drift regimes (a planted trend edge). Used only by tests.
    Prefix-stable: the first m bars are identical for any n >= m (independent RNG streams, sequential draws)."""
    ss = np.random.SeedSequence(seed).spawn(5)
    g_flip, g_noise, g_hi, g_lo, g_v = (np.random.default_rng(s) for s in ss)
    flips = g_flip.random(n) < regime_flip
    state = np.where(np.cumsum(flips) % 2 == 0, 1, -1)
    rets = state * drift + g_noise.normal(0, sigma, n)
    c = start * np.exp(np.cumsum(rets))
    o = np.r_[start, c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(g_hi.normal(0, sigma / 3, n)))
    l = np.minimum(o, c) * (1 - np.abs(g_lo.normal(0, sigma / 3, n)))
    v = g_v.uniform(500, 1500, n)
    t = t0 + np.arange(n) * 3_600_000
    return np.column_stack([t, o, h, l, c, v, t + 3_599_999, v * c, np.full(n, 100.0), v * 0.5, v * c * 0.5, np.zeros(n)])


def write_dataset(home, symbol, k, funding=True, cls=None):
    import pathlib
    d = pathlib.Path(home) / "data"; d.mkdir(parents=True, exist_ok=True)
    ft = k[::8, 0] + 0.0
    f = np.column_stack([ft, np.full(len(ft), 0.0001)]) if funding else np.empty((0, 2))
    kw = dict(k=k, f=f)
    if cls:
        kw["cls"] = np.array(cls)
    np.savez_compressed(d / f"{symbol}_1h.npz", **kw)
