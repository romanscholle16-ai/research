"""Market data: resumable checksum-verified downloads (Binance public archive), dataset manifests,
integrity checks, aligned multi-symbol Market objects and protected-holdout access control."""
import datetime as dt, hashlib, io, json, os, time, urllib.error, urllib.request, zipfile
import numpy as np

from . import config, db, log

LG = log.get("data")
BASE = "https://data.binance.vision/data/futures/um"
TF_MS = {"1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}
KCOLS = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbq", "ig"]


# ------------------------------------------------------------------------------ downloading ---
def _http(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "nexus-v2-research"})
    return urllib.request.urlopen(req, timeout=timeout)


def download_file(url, dest, retries=4, sleep=time.sleep):
    """Resumable download to dest.part -> sha256-verify against <url>.CHECKSUM -> atomic rename.
    Returns 'ok' | 'missing' | 'failed'. An interrupted .part file is resumed with a Range request."""
    part = str(dest) + ".part"
    for attempt in range(retries):
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            hdr = {"User-Agent": "nexus-v2-research"}
            if have:
                hdr["Range"] = f"bytes={have}-"
            try:
                r = _http(url, headers=hdr)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return "missing"
                if e.code == 416:  # part already complete
                    r = None
                elif e.code in (418, 429, 503):
                    sleep(min(60, 2 ** (attempt + 1)))
                    continue
                else:
                    raise
            if r is not None:
                mode = "ab" if (have and r.status == 206) else "wb"
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            sha = hashlib.sha256(open(part, "rb").read()).hexdigest()
            try:
                exp = _http(url + ".CHECKSUM").read().decode().split()[0].strip().lower()
            except urllib.error.HTTPError:
                exp = None
            if exp is not None and exp != sha:
                os.remove(part)
                raise IOError(f"checksum mismatch for {url}")
            os.replace(part, dest)
            return "ok:" + ("verified" if exp else "unchecked")
        except Exception as e:  # network/IO: bounded exponential backoff
            LG.warning("download attempt %d failed for %s: %s", attempt + 1, url, e)
            sleep(min(30, 2 ** (attempt + 1)))
    return "failed"


def _months(start_month, upto=None):
    y, m = map(int, start_month.split("-"))
    now = upto or dt.datetime.now(dt.timezone.utc)
    while (y, m) <= (now.year, now.month):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def sync_symbol(conn, symbol, tf="1h", start_month=None, kinds=("klines", "fundingRate"), sleep=time.sleep, now=None, abort=None):
    """Incrementally download all missing monthly files (+ daily files for the current month)."""
    start_month = start_month or config.DATA["start_month"]
    now = now or dt.datetime.now(dt.timezone.utc)
    cur = f"{now.year:04d}-{now.month:02d}"
    stats = {"ok": 0, "skipped": 0, "missing": 0, "failed": 0}
    for kind in kinds:
        d = config.path("data", "raw", symbol, kind, tf if kind == "klines" else "x", "f.zip").parent
        for ym in _months(start_month, now):
            if ym == cur:
                days = [(now - dt.timedelta(days=k)).strftime("%Y-%m-%d") for k in range(now.day - 1, 1 - 1, -1)]
                days = [x for x in days if x.startswith(ym) and x < now.strftime("%Y-%m-%d")]
                targets = [("daily", x) for x in days]
            else:
                targets = [("monthly", ym)]
            for gran, per in targets:
                nm = f"{symbol}-{tf}-{per}" if kind == "klines" else f"{symbol}-fundingRate-{per}"
                if gran == "daily" and kind == "fundingRate":
                    continue  # funding is published monthly only
                if abort and abort():                 # stop requested: finished files are already recorded; resume later
                    stats["aborted"] = True
                    return stats
                dest = d / f"{nm}.zip"
                row = conn.execute("SELECT verified FROM data_files WHERE path=?", (str(dest),)).fetchone()
                if row is not None and dest.exists():
                    stats["skipped"] += 1
                    continue
                sub = f"{kind}/{symbol}/{tf}" if kind == "klines" else f"{kind}/{symbol}"
                url = f"{BASE}/{gran}/{sub}/{nm}.zip"
                r = download_file(url, dest, sleep=sleep)
                if r.startswith("ok"):
                    with db.tx(conn):
                        conn.execute("INSERT OR REPLACE INTO data_files VALUES(?,?,?,?,?,?,?,?)",
                                     (str(dest), symbol, kind, per, hashlib.sha256(dest.read_bytes()).hexdigest(),
                                      int(r.endswith("verified")), dest.stat().st_size, time.time()))
                    stats["ok"] += 1
                else:
                    stats[r] = stats.get(r, 0) + 1
                sleep(0.05)
    return stats


# --------------------------------------------------------------------------------- parsing -----
def _read_zip_csv(p):
    with zipfile.ZipFile(p) as z:
        raw = z.read(z.namelist()[0]).decode()
    rows = []
    for ln in raw.splitlines():
        if not ln or not (ln[0].isdigit()):
            continue  # header line
        rows.append(ln.split(","))
    return rows


def parse_klines(files):
    parts = []
    for p in files:
        try:
            rows = _read_zip_csv(p)
        except zipfile.BadZipFile:
            LG.error("corrupt zip %s", p)
            continue
        if rows:
            parts.append(np.array(rows, dtype=np.float64)[:, :11])
    if not parts:
        return np.empty((0, 11))
    a = np.vstack(parts)
    if a[0, 0] > 1e14:  # microseconds -> milliseconds
        a[:, 0] //= 1000
    return a


def parse_funding(files):
    out = []
    for p in files:
        try:
            for r in _read_zip_csv(p):
                out.append((float(r[0]), float(r[-1])))
        except zipfile.BadZipFile:
            LG.error("corrupt zip %s", p)
    if not out:
        return np.empty((0, 2))
    a = np.array(sorted(set(out)))
    return a


def validate_klines(a, tf_ms):
    """Sort, dedupe, and report integrity. Returns (clean_array, stats)."""
    n0 = len(a)
    a = a[np.argsort(a[:, 0], kind="stable")]
    keep = np.concatenate(([True], np.diff(a[:, 0]) > 0)) if len(a) else np.array([], bool)
    dupes = int((~keep).sum())
    a = a[keep]
    o, h, l, c, v = a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
    bad = ~((o > 0) & (h >= np.maximum(o, c) - 1e-9) & (l <= np.minimum(o, c) + 1e-9) & (l > 0) & (v >= 0)
            & np.isfinite(a[:, :6]).all(axis=1))
    bad_rows = int(bad.sum())
    a = a[~bad]
    d = np.diff(a[:, 0])
    gaps = int(((d > tf_ms)).sum())
    missing = int(((d[d > tf_ms] // tf_ms) - 1).sum()) if gaps else 0
    return a, {"rows_in": n0, "rows": len(a), "dupes": dupes, "bad_rows": bad_rows, "gap_events": gaps,
               "missing_bars": missing, "gap_frac": missing / max(1, len(a) + missing)}


def build_dataset(conn, symbol, tf="1h"):
    """Merge raw files into one validated npz + manifest row. Returns manifest dict or None."""
    raw = config.path("data", "raw", symbol, "klines", tf, "x.zip").parent
    files = sorted(raw.glob("*.zip"))
    if not files:
        return None
    a = parse_klines(files)
    a, st = validate_klines(a, TF_MS[tf])
    if len(a) == 0:
        return None
    fr_dir = config.path("data", "raw", symbol, "fundingRate", "x", "x.zip").parent
    fr = parse_funding(sorted(fr_dir.glob("*.zip")))
    out = config.path("data", f"{symbol}_{tf}.npz")
    tmp = str(out) + ".tmp.npz"
    np.savez_compressed(tmp, k=a, f=fr)
    ver = hashlib.sha256(a.tobytes() + fr.tobytes()).hexdigest()[:12]
    os.replace(tmp, out)
    man = {"symbol": symbol, "timeframe": tf, "source": "data.binance.vision futures/um (checksum-verified zips)",
           "timezone": "UTC (epoch ms, bar open time)", "schema": KCOLS, "coverage_utc": [
               dt.datetime.fromtimestamp(a[0, 0] / 1000, dt.timezone.utc).isoformat(),
               dt.datetime.fromtimestamp(a[-1, 0] / 1000, dt.timezone.utc).isoformat()],
           "funding_events": int(len(fr)), "funding_source": "binance fundingRate archive (proxy venue)",
           "unavailable": ["order-book", "liquidations", "open-interest (not ingested)", "BTCC-native prices"], **st}
    with db.tx(conn):
        conn.execute("INSERT INTO datasets(symbol,kind,timeframe,source,retrieved_at,rows,start_ts,end_ts,gaps,dupes,bad_rows,"
                     "checksum,version,data_class,path,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (symbol, "klines", tf, man["source"], time.time(), len(a), int(a[0, 0]), int(a[-1, 0]), st["gap_events"],
                      st["dupes"], st["bad_rows"], ver, ver, "verified", str(out), json.dumps(man)))
        db.event(conn, "data", "data", f"dataset {symbol} {tf} v{ver} rows={len(a)} gap_frac={st['gap_frac']:.4%}")
    return {**man, "version": ver}


# --------------------------------------------------------------------------------- Market ------
class Market:
    """Aligned OHLCV (+taker-buy volume, +other-symbol close, +funding) for one primary symbol/timeframe.
    Bar i is labelled by its OPEN time t[i]; its values become known at t[i]+tf_ms (bar close)."""
    __slots__ = ("symbol", "tf", "tf_ms", "t", "o", "h", "l", "c", "v", "tbv", "xc", "xo", "fund_t", "fund_r",
                 "version", "data_class", "funding_known", "uid", "other")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.uid = f"{self.symbol}|{self.tf}|{self.version}|{len(self.t)}|{int(self.t[0])}"

    def region(self, i0, i1):
        """View [i0,i1). Indicators are computed on a prefix [0,i1) by callers, so no future data is visible."""
        sl = slice(i0, i1)
        t0 = self.t[0]
        ft = self.fund_t
        fsel = (ft >= self.t[i0]) & (ft <= self.t[i1 - 1] + self.tf_ms) if len(ft) else np.zeros(0, bool)
        m = Market(symbol=self.symbol, tf=self.tf, tf_ms=self.tf_ms, t=self.t[sl], o=self.o[sl], h=self.h[sl],
                   l=self.l[sl], c=self.c[sl], v=self.v[sl], tbv=self.tbv[sl],
                   xc=None if self.xc is None else self.xc[sl], xo=None if self.xo is None else self.xo[sl],
                   fund_t=ft[fsel], fund_r=self.fund_r[fsel], version=self.version, data_class=self.data_class,
                   funding_known=self.funding_known, other=self.other)
        m.uid = f"{self.uid}|{i0}:{i1}"
        return m

    def prefix(self, i1):
        return self.region(0, i1)

    def __len__(self):
        return len(self.t)


def _aggregate(k, tf_ms, base_ms=3_600_000):
    if tf_ms == base_ms:
        return k
    g = (k[:, 0] // tf_ms).astype(np.int64)
    uniq, idx, cnt = np.unique(g, return_index=True, return_counts=True)
    full = cnt == tf_ms // base_ms  # drop incomplete groups -> no partial-bar leakage
    # contiguity check: group must span exactly tf
    rows = []
    for u, s, c in zip(uniq, idx, cnt):
        if c != tf_ms // base_ms:
            continue
        blk = k[s:s + c]
        if blk[-1, 0] - blk[0, 0] != tf_ms - base_ms:
            continue
        rows.append([blk[0, 0], blk[0, 1], blk[:, 2].max(), blk[:, 3].min(), blk[-1, 4], blk[:, 5].sum(),
                     blk[-1, 6], blk[:, 7].sum(), blk[:, 8].sum(), blk[:, 9].sum(), blk[:, 10].sum()])
    return np.array(rows)


_CACHE = {}


def load_market(symbol, tf, other=None, base_tf="1h", path_override=None) -> Market:
    """Load validated data; optional second symbol joined on identical timestamps (inner join)."""
    key = (symbol, tf, other, os.path.getmtime(path_override or config.path("data", f"{symbol}_{base_tf}.npz")))
    if key in _CACHE:
        return _CACHE[key]
    with np.load(path_override or config.path("data", f"{symbol}_{base_tf}.npz")) as z:    # close the handle (Windows: blocks replace)
        kraw, fr = z["k"], z["f"]
        cls = str(z["cls"]) if "cls" in z.files else "verified"
    k = _aggregate(kraw, TF_MS[tf])
    xc = xo = None
    if other:
        with np.load(config.path("data", f"{other}_{base_tf}.npz")) as zo:
            kor = zo["k"]
        ko = _aggregate(kor, TF_MS[tf])
        common, ia, ib = np.intersect1d(k[:, 0], ko[:, 0], return_indices=True)
        if len(common) < 0.98 * len(k):
            raise ValueError("insufficient timestamp overlap between symbols")
        k, ko = k[ia], ko[ib]
        xc, xo = ko[:, 4].copy(), ko[:, 1].copy()
    ver = hashlib.sha256(k.tobytes() + fr.tobytes()).hexdigest()[:12]
    m = Market(symbol=symbol, tf=tf, tf_ms=TF_MS[tf], t=k[:, 0].astype(np.int64), o=k[:, 1], h=k[:, 2], l=k[:, 3],
               c=k[:, 4], v=k[:, 5], tbv=k[:, 9] if k.shape[1] > 9 else np.zeros(len(k)), xc=xc, xo=xo,
               fund_t=fr[:, 0].astype(np.int64) if len(fr) else np.zeros(0, np.int64),
               fund_r=fr[:, 1] if len(fr) else np.zeros(0), version=ver,
               data_class=cls, funding_known=len(fr) > 0, other=other)
    if len(_CACHE) > 8:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = m
    return m


# ---------------------------------------------------------------------- splits & the holdout ----
def get_split(conn, symbol, tf, mkt: Market):
    """Fixed chronological split recorded at first sight; later data lies beyond holdout_end (= forward)."""
    key = f"{symbol}|{tf}"
    r = conn.execute("SELECT * FROM data_splits WHERE key=?", (key,)).fetchone()
    if r is None:
        n = len(mkt)
        D = config.DATA
        ex, dv = int(n * D["explore_frac"]), int(n * D["dev_frac"])
        with db.tx(conn):
            conn.execute("INSERT OR IGNORE INTO data_splits VALUES(?,?,?,?,?,?,?)",
                         (key, int(mkt.t[0]), int(mkt.t[ex]), int(mkt.t[dv]), int(mkt.t[-1]) + mkt.tf_ms, time.time(),
                          mkt.version))
        r = conn.execute("SELECT * FROM data_splits WHERE key=?", (key,)).fetchone()
    t = mkt.t
    return {"explore_end": int(np.searchsorted(t, r["explore_end_ts"])), "dev_end": int(np.searchsorted(t, r["dev_end_ts"])),
            "holdout_end": int(np.searchsorted(t, r["holdout_end_ts"])), "n": len(t),
            "explore_end_ts": r["explore_end_ts"], "dev_end_ts": r["dev_end_ts"], "holdout_end_ts": r["holdout_end_ts"]}


class HoldoutDenied(Exception):
    pass


def holdout_region(conn, mkt, split, strategy_id, purpose, caller, allow_when=None):
    """Only the validation module may call this. Enforces: one look per candidate, a global budget, and that the
    candidate has already passed walk-forward + robustness. Every attempt (granted or not) is logged."""
    T = config.THRESHOLDS
    def deny(why):
        conn.execute("INSERT INTO holdout_access(strategy_id,ts,purpose,caller,dataset_version,passed) VALUES(?,?,?,?,?,-1)",
                     (strategy_id, time.time(), f"DENIED: {why}", caller, mkt.version))
        raise HoldoutDenied(why)
    if caller != "validation.holdout_stage":
        deny("unauthorised caller")
    prior = conn.execute("SELECT COUNT(*) FROM holdout_access WHERE strategy_id=? AND passed>=0", (strategy_id,)).fetchone()[0]
    if prior:
        deny("candidate already evaluated on holdout (single look)")
    total = conn.execute("SELECT COUNT(*) FROM holdout_access WHERE passed>=0").fetchone()[0]
    if total >= T["ho_max_evaluations_total"]:
        deny("global holdout budget exhausted")
    ok = {r["stage"] for r in conn.execute("SELECT stage FROM validations WHERE strategy_id=? AND passed=1", (strategy_id,))}
    if not {"walkforward", "robustness"} <= ok:
        deny("walk-forward and robustness must pass first")
    conn.execute("INSERT INTO holdout_access(strategy_id,ts,purpose,caller,dataset_version,passed) VALUES(?,?,?,?,?,0)",
                 (strategy_id, time.time(), purpose, caller, mkt.version))
    return split["dev_end"], split["holdout_end"]
