"""Historical data: download (Binance USD-M public archive), quality audit, resample, synthetic fixtures.

Source note: BTCC publishes no bulk historical archive reachable by this tool. Binance USD-M public
archive (data.binance.vision) is used as a *proxy* for BTCUSDT/ETHUSDT perpetual price action.
This is a documented limitation (docs/LIMITATIONS.md). Nothing is invented: gaps are recorded, not filled.
"""
from __future__ import annotations
import io, time, zipfile, urllib.request, urllib.error
from pathlib import Path
import numpy as np, pandas as pd
from .config import home_dir

BASE = "https://data.binance.vision/data/futures/um/monthly"
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
        "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def data_dir() -> Path:
    p = home_dir() / "data"; p.mkdir(parents=True, exist_ok=True); return p


def _get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(2 ** i)
        except Exception:
            time.sleep(2 ** i)
    raise ConnectionError(url)


def months(start="2019-09", end=None):
    end = end or time.strftime("%Y-%m", time.gmtime(time.time() - 40 * 86400))  # last complete archived month
    return [str(p) for p in pd.period_range(start, end, freq="M")]


def download_month(symbol, kind, tf, ym):
    """kind: 'klines'|'fundingRate'. Cached as parquet-free CSV.gz. Returns path or None (not available)."""
    d = data_dir() / symbol / kind; d.mkdir(parents=True, exist_ok=True)
    out = d / f"{tf if kind=='klines' else 'f'}-{ym}.csv.gz"
    if out.exists(): return out
    name = f"{symbol}-{tf}-{ym}" if kind == "klines" else f"{symbol}-fundingRate-{ym}"
    url = f"{BASE}/{kind}/{symbol}/{tf + '/' if kind=='klines' else ''}{name}.zip"
    raw = _get(url)
    if raw is None: return None
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(z.open(z.namelist()[0]))
    if kind == "klines":
        df = df[pd.to_numeric(df.iloc[:, 0], errors="coerce").notna()]
        df = df.iloc[:, :11]
    df.to_csv(out, index=False, compression="gzip")
    return out


def _read_one(f, kind):
    df = pd.read_csv(f, header=None, dtype=str)
    if not df.empty and pd.to_numeric(df.iloc[0, 0], errors="coerce") != pd.to_numeric(df.iloc[0, 0], errors="coerce"):
        df = df.iloc[1:]                       # drop textual header row (archive files vary: some have none)
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=[0])


def sync_daily(symbol, start="2026-10-01", end=None, log=print):
    """Download daily 1m kline archives (post-monthly-archive 'forward' data). Returns number of new days."""
    end = pd.Timestamp(end or time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400)), tz="UTC").normalize()
    d = data_dir() / symbol / "klines"; d.mkdir(parents=True, exist_ok=True); new = 0
    for day in pd.date_range(pd.Timestamp(start, tz="UTC"), end, freq="D"):
        ds = f"{day:%Y-%m-%d}"; out = d / f"1m-{ds}.csv.gz"
        if out.exists(): continue
        raw = _get(f"{BASE.replace('monthly', 'daily')}/klines/{symbol}/1m/{symbol}-1m-{ds}.zip")
        if raw is None: continue            # not published yet
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]), header=None, dtype=str)
        df = df[pd.to_numeric(df.iloc[:, 0], errors="coerce").notna()].iloc[:, :11]
        df.to_csv(out, index=False, compression="gzip"); new += 1
    return new


def _read_all(symbol, kind, tf):
    d = data_dir() / symbol / kind
    pat = f"{tf}-*.csv.gz" if kind == "klines" else "f-*.csv.gz"
    fs = sorted(d.glob(pat)) if d.exists() else []
    if not fs: return None
    parts = []
    for f in fs:                                   # per-file parse cache: a daily sync only parses the new day files
        pk = f.with_suffix(".pkl")
        if pk.exists() and pk.stat().st_mtime >= f.stat().st_mtime: parts.append(pd.read_pickle(pk)); continue
        part = _read_one(f, kind); part.to_pickle(pk); parts.append(part)
    out = pd.concat(parts, ignore_index=True)
    out.columns = (COLS[: out.shape[1]] if kind == "klines" else ["calc_time", "funding_interval_hours", "last_funding_rate"][: out.shape[1]])
    return out


def fetch_symbol(symbol, base_tf="1m", start="2019-09", log=print, max_months=None):
    ms = months(start)[: max_months]
    for ym in ms:
        for kind, tf in (("klines", base_tf), ("fundingRate", "")):
            try:
                download_month(symbol, kind, tf, ym)
            except Exception as e:
                log(f"download failed {symbol} {kind} {ym}: {e}")
        log(f"{symbol} {ym} ok")


def resample(df1: pd.DataFrame, tf: str) -> pd.DataFrame:
    m = TF_MIN[tf]
    if m == 1: return df1
    g = df1.groupby(df1.index.floor(f"{m}min"))
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "quote_volume": g["quote_volume"].sum(), "count": g["count"].sum(),
        "taker_buy_volume": g["taker_buy_volume"].sum(), "n": g["close"].count()})
    out = out[out["n"] >= max(1, int(m * 0.8))].drop(columns="n")  # drop badly incomplete bars
    return out


def load_real(symbol, tf, base_tf="1m"):
    """Return (df, meta). df: UTC index; columns OHLCV, taker_buy_volume, funding (rate of the 8h period
    applicable at each bar, only known values - forward filled from the *settled* time)."""
    cdir = data_dir() / symbol
    srcs = sorted((cdir / "klines").glob(f"{base_tf}-*.csv.gz")) + sorted((cdir / "fundingRate").glob("f-*.csv.gz"))
    if not srcs: raise FileNotFoundError(f"no data for {symbol}; run data fetch")
    sig = f"{len(srcs)}-{int(max(f.stat().st_mtime for f in srcs))}"
    cache = cdir / f"cache-{tf}-{sig}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    k = _read_all(symbol, "klines", base_tf)
    k = k.drop_duplicates("open_time").sort_values("open_time")
    k.index = pd.to_datetime(k["open_time"], unit="ms", utc=True)
    k = k[["open", "high", "low", "close", "volume", "quote_volume", "count", "taker_buy_volume"]].astype(float)
    df = resample(k, tf) if tf != base_tf else k
    f = _read_all(symbol, "fundingRate", "")
    has_f = f is not None and len(f) > 0
    df["funding"] = 0.0
    if has_f:
        f = f.drop_duplicates("calc_time").sort_values("calc_time")
        fs = pd.Series(f["last_funding_rate"].values, index=pd.to_datetime(f["calc_time"], unit="ms", utc=True))
        # funding rate becomes known at settlement time; reindex with ffill (no look-ahead)
        df["funding"] = fs.reindex(df.index.union(fs.index)).ffill().reindex(df.index).fillna(0.0)
    step = pd.Timedelta(minutes=TF_MIN[tf])
    gaps = int((df.index.to_series().diff() > step * 1.5).sum())
    meta = dict(id=f"{symbol}-{tf}-{df.index[0]:%Y%m%d}-{df.index[-1]:%Y%m%d}-{len(df)}", symbol=symbol, timeframe=tf,
                source="data.binance.vision USD-M monthly archive (proxy for BTCC)", start_ts=int(df.index[0].timestamp()),
                end_ts=int(df.index[-1].timestamp()), rows=len(df), gaps=gaps, has_funding=int(has_f), has_taker=1,
                synthetic=0, notes="No OI/liquidation/orderbook/bid-ask history in this dataset; taker-buy volume used as CVD proxy.")
    for old in cdir.glob(f"cache-{tf}-*.pkl"): old.unlink()
    pd.to_pickle((df, meta), cache)
    return df, meta


def synthetic(symbol="SYNTH", tf="1h", n=20000, seed=0, with_edge=False, edge=0.1) -> tuple:
    """Deterministic synthetic fixture (GBM + vol clustering). FOR TESTS ONLY - flagged synthetic=1."""
    rng = np.random.default_rng(seed)
    vol = 0.004 * np.exp(np.cumsum(rng.normal(0, 0.02, n)) * 0.3)
    vol = np.clip(vol, 0.001, 0.03)
    r = rng.normal(0, 1, n) * vol
    if with_edge:
        r[1:] += edge * r[:-1]  # serial correlation (momentum) - synthetic edge for pipeline tests
    c = 20000 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    spread = np.abs(rng.normal(0, 1, n)) * vol * 0.5
    h = np.maximum(o, c) * (1 + spread); l = np.minimum(o, c) * (1 - spread)
    v = rng.lognormal(5, 0.5, n)
    tb = v * np.clip(0.5 + rng.normal(0, 0.08, n), 0.05, 0.95)
    idx = pd.date_range("2021-01-01", periods=n, freq=f"{TF_MIN[tf]}min", tz="UTC")
    df = pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v, quote_volume=v * c, count=v * 10, taker_buy_volume=tb,
                           funding=0.0001 * np.sin(np.arange(n) / 500)), index=idx)
    meta = dict(id=f"{symbol}-{tf}-synthetic-{seed}-{n}", symbol=symbol, timeframe=tf, source="SYNTHETIC FIXTURE", start_ts=int(idx[0].timestamp()),
                end_ts=int(idx[-1].timestamp()), rows=n, gaps=0, has_funding=1, has_taker=1, synthetic=1, notes="synthetic test data")
    return df, meta
