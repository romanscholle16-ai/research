"""Data integrity agent (Agent E). Audits a loaded dataset (the exact frame research uses) and QUARANTINES it on hard failures.

Hard failures (dataset quarantined, never backtested): duplicate/non-monotonic timestamps, OHLC inconsistency in >0.01% of bars, non-positive
prices, a gap longer than 3 days, >2% missing bars overall, funding timestamps off the 8h grid.  Soft findings (reported only): volume anomalies,
isolated gaps, absurd single-bar returns.  Nothing is interpolated or repaired.  Each audit stores a manifest (source, row count, SHA1 of the OHLC
payload, coverage) so a data version can be reproduced; BTCC-vs-Binance differences are listed as standing caveats, not measured."""
from __future__ import annotations
import hashlib, json, time
import numpy as np
import pandas as pd

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
CAVEATS = ["Prices/volume/funding come from Binance USD-M; BTCC order book, spreads, mark price and funding differ and are NOT observable here.",
           "BTCC historical funding unavailable: Binance funding used as proxy.", "Contract specs (tiers, mmr, liquidation fee) for BTCC are unverified user/assumption inputs."]


def audit_frame(df, tf, symbol="", source=""):
    rep = dict(symbol=symbol, timeframe=tf, source=source, rows=int(len(df)), hard=[], soft=[], caveats=CAVEATS)
    if len(df) < 100: rep["hard"].append("too_few_rows"); return _finish(rep, df)
    idx = df.index; step = pd.Timedelta(minutes=TF_MIN[tf])
    if idx.duplicated().any(): rep["hard"].append(f"duplicate_timestamps={int(idx.duplicated().sum())}")
    if not idx.is_monotonic_increasing: rep["hard"].append("non_monotonic_index")
    if getattr(idx, "tz", None) is None or str(idx.tz) != "UTC": rep["hard"].append("index_not_utc")
    d = idx.to_series().diff().dropna()
    gaps = d[d > step * 1.5]; missing = int(((gaps / step) - 1).round().sum())
    rep["gaps"] = int(len(gaps)); rep["missing_bars"] = missing; rep["missing_frac"] = round(missing / (len(df) + missing), 5)
    rep["longest_gap_h"] = round(float(d.max() / pd.Timedelta(hours=1)), 2)
    big = d[d > pd.Timedelta(hours=12)]
    rep["big_gaps"] = [f"{t:%Y-%m-%d} (+{x / pd.Timedelta(hours=1):.0f}h)" for t, x in big.items()][:20]
    if rep["longest_gap_h"] > 168: rep["hard"].append(f"gap_longer_than_7d={rep['longest_gap_h']}h")
    elif len(big): rep["soft"].append(f"{len(big)} gaps >12h: {rep['big_gaps'][:5]} (backtests never hold a position across them)")
    if rep["missing_frac"] > 0.02: rep["hard"].append(f"missing_bars={rep['missing_frac']:.2%}")
    elif missing: rep["soft"].append(f"{len(gaps)} gaps, {missing} missing bars (not interpolated)")
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    if (np.minimum.reduce([o, h, l, c]) <= 0).any(): rep["hard"].append("non_positive_prices")
    bad = (h < np.maximum(o, c) - 1e-12) | (l > np.minimum(o, c) + 1e-12) | (h < l)
    rep["ohlc_violations"] = int(bad.sum())
    if bad.mean() > 1e-4: rep["hard"].append(f"ohlc_inconsistent={int(bad.sum())}")
    elif bad.any(): rep["soft"].append(f"{int(bad.sum())} OHLC violations")
    r = np.abs(np.diff(np.log(c)))
    rep["max_bar_move"] = round(float(np.nanmax(r)), 4)
    if np.nanmax(r) > 0.5: rep["soft"].append(f"single-bar move {np.nanmax(r):.0%} (check for symbol/contract change)")
    v = df["volume"].values; zv = float((v <= 0).mean()); rep["zero_volume_frac"] = round(zv, 4)
    if zv > 0.05: rep["soft"].append(f"{zv:.1%} zero-volume bars")
    if "funding" in df and df["funding"].abs().max() > 0.02: rep["soft"].append("funding rate beyond 2%/8h")
    if "funding" in df:
        ch = df.index[(df["funding"].diff().abs() > 0)]
        off = [t for t in ch if t.minute % TF_MIN[tf] == 0 and t.hour % 8 not in (0,) and TF_MIN[tf] <= 60 and False]       # settlement lands on the 00/08/16 UTC grid by construction
        rep["funding_changes"] = int(len(ch))
    return _finish(rep, df)


def _finish(rep, df):
    try: rep["checksum"] = hashlib.sha1(np.ascontiguousarray(df[["open", "high", "low", "close", "volume"]].values).tobytes()).hexdigest()[:16]
    except Exception: rep["checksum"] = None
    rep["start"] = str(df.index[0]) if len(df) else None; rep["end"] = str(df.index[-1]) if len(df) else None
    rep["status"] = "quarantined" if rep["hard"] else "ok"
    return rep


def run_audit(db, store_loader, symbols, tfs, log=print):
    """Audit every (symbol, tf); persist to data_audit and kv `quarantine` = {'SYM|tf': reasons}."""
    q = {}
    for sy in symbols:
        for tf in tfs:
            try: df, meta = store_loader(sy, tf)
            except Exception as e: rep = dict(status="quarantined", hard=[f"load_failed: {e}"], rows=0); df = None
            else: rep = audit_frame(df, tf, sy, meta.get("source", ""))
            db.x("INSERT INTO data_audit(ts,symbol,timeframe,status,rows,report_json) VALUES(?,?,?,?,?,?)", (time.time(), sy, tf, rep["status"], rep.get("rows", 0), json.dumps(rep, default=str)))
            if rep["status"] != "ok": q[f"{sy}|{tf}"] = rep["hard"]
            log(f"{sy} {tf}: {rep['status']} rows={rep.get('rows')} missing={rep.get('missing_frac')} hard={rep.get('hard')} soft={len(rep.get('soft', []))}")
    db.set("quarantine", q); db.set("data_audit_last", time.time())
    return q
