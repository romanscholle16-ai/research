"""Null calibration: run the FULL pipeline on data where no strategy can have genuine skill and measure the false-discovery rate.

Null frames are built from REAL bars: block-resample bars (relative OHLC vs previous close, volume, taker volume) in blocks of `--block`
bars, rebuild the price path, keep the real timestamps and funding. `--demean` removes the sample drift (no beta available to long
strategies); without it the null keeps the real market drift (tests beta handling).
Usage: null_calibration.py --n 1200 --demean|--drift --out result.json   (uses a fresh temporary research DB)"""
import sys, os, json, time, argparse, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=1200); ap.add_argument("--block", type=int, default=24)
ap.add_argument("--demean", action="store_true"); ap.add_argument("--seed", type=int, default=0); ap.add_argument("--out", required=True)
ap.add_argument("--tfs", default="1h,4h"); a = ap.parse_args()
real_home = os.environ["STRATEGYLAB_HOME"]; tmp = tempfile.mkdtemp(prefix="null_")
import numpy as np, pandas as pd
from strategylab import data as D
from strategylab.config import Settings
os.environ["STRATEGYLAB_HOME"] = real_home          # read real data from here ...
cache = {}
def null_loader(symbol, tf):
    k = (symbol, tf)
    if k in cache: return cache[k]
    df, meta = D.load_real(symbol, tf)
    rng = np.random.default_rng(a.seed + sum(map(ord, symbol)) + len(tf)); n = len(df)
    pc = df["close"].shift(1).bfill().to_numpy()
    rel = np.column_stack([df[c].to_numpy() / pc for c in ("open", "high", "low", "close")])
    if a.demean: rel = rel * np.exp(-np.log(rel[:, 3]).mean())            # remove drift per bar
    starts = rng.integers(0, n - a.block, size=n // a.block + 2)
    idx = np.concatenate([np.arange(s, s + a.block) for s in starts])[:n]
    r = rel[idx]; path = np.cumprod(r[:, 3]) * df["close"].iloc[0]; prev = np.r_[df["close"].iloc[0], path[:-1]]
    out = df.copy()
    for j, c in enumerate(("open", "high", "low", "close")): out[c] = r[:, j] * prev
    for c in ("volume", "quote_volume", "count", "taker_buy_volume"): out[c] = df[c].to_numpy()[idx]
    meta = dict(meta, id=meta["id"] + f"-NULL{a.seed}{'D' if a.demean else ''}", synthetic=1, source="NULL RESAMPLE OF REAL BARS", notes="null calibration")
    cache[k] = (out, meta); return cache[k]
os.symlink(os.path.join(real_home, "data"), os.path.join(tmp, "data"))
os.environ["STRATEGYLAB_HOME"] = tmp                  # ... results go to a throwaway DB; data via symlink
from strategylab.db import DB
from strategylab.engine import DataStore, run_experiment
from strategylab.agents import Supervisor
st = Settings(); st.timeframes = a.tfs.split(","); st.mc_paths = 400
db = DB(); store = DataStore(st, null_loader, max_items=6); sup = Supervisor(db, st, None, store)
t0 = time.time(); done = 0
while done < a.n:
    for j in sup.plan(8, use_ai=False):
        run_experiment(db, st, store, j); done += 1
res = dict(n=done, demean=a.demean, block=a.block, seconds=round(time.time() - t0), counts=db.counts(1), reached_final=db.q("SELECT COUNT(DISTINCT strategy_id||symbol||timeframe) n FROM experiments WHERE stage='final'")[0]["n"],
           by_family=db.q("SELECT s.family f, COUNT(*) n, SUM(ss.status='PROMISING') p FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id GROUP BY 1"))
n = res["counts"].get("experiments", 1)
res["promising_rate"] = res["counts"].get("PROMISING", 0) / max(done, 1); res["qualified"] = res["counts"].get("BACKTEST_QUALIFIED", 0)
json.dump(res, open(a.out, "w"), indent=1); print(json.dumps({k: res[k] for k in ("n", "seconds", "counts", "reached_final", "promising_rate", "qualified")}))
