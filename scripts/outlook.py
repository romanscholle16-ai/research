"""What the data supports: (1) realistic-return ladder from locked-test finalists, (2) funding-income ceiling per market. -> docs/OUTLOOK.md (descriptive; never used for selection)."""
import json, time, glob, gzip
import numpy as np, pandas as pd
from pathlib import Path
from strategylab.db import DB
from strategylab.data import data_dir
from strategylab.config import Settings

d = DB(); st = Settings.load(); out = [f"# Outlook — what this data can realistically support (generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})\n"]
rows = d.q("SELECT metrics_json m FROM experiments WHERE stage='final' AND synthetic=0 AND timeframe!='4h' AND finished>?", (time.time() - 3 * 86400,))
mo2, mo5, tt, rr_ = [], [], [], []
for r in rows:
    m = json.loads(r["m"]); t = m.get("test") or {}; c = m.get("capital_test") or {}
    if t.get("n", 0) < 20: continue
    tpm = c.get("median_trades_per_month") or 0
    rr_.append(t["mean_R"]); tt.append(t["t"]); mo2.append(tpm * t["mean_R"] * 0.02); mo5.append(tpm * t["mean_R"] * 0.05)
mo2, mo5, tt, rr_ = map(np.array, (mo2, mo5, tt, rr_))
out.append("## 1. Return ladder from locked-test finalists (last 3 days, non-4h) -- plain arithmetic: trades/month x edge per trade x risk per trade\n")
if len(rr_):
    pos = rr_ > 0
    out.append(f"- {len(rr_)} finalists with >=20 locked-test trades; edge per trade > 0 in {100*pos.mean():.0f}%; statistically significant (t>2) in {int((tt>2).sum())}.")
    out.append(f"- Monthly return if risking 2% of equity per trade: median {100*np.median(mo2):.2f}%, best {100*mo2.max():.1f}%; at 5% risk: median {100*np.median(mo5):.2f}%, best {100*mo5.max():.1f}% (single best of ~{len(rr_)}, i.e. selection-inflated).")
    out.append(f"- The target needs about +61% per month (compounded) to reach $1,000/month withdrawable within 6 months. Nothing in the data comes within a factor of ~10 of that without leverage that makes ruin likely.")
out.append("- (The $300 capital simulation in the final stage is not used here: its wealth outputs could not be reconciled with these per-trade numbers and need an audit.)")
out.append("\n## 2. Funding-rate income ceiling (hold the paying side of perpetual funding; ignores price risk and fees)\n\n| market | mean funding / 8h | annualised | best 90d window (annualised) | worst 90d window (annualised) | share of 8h periods paying shorts |\n|---|---|---|---|---|---|")
for sy in st.symbols:
    fs = sorted((data_dir() / sy / "fundingRate").glob("f-*.csv.gz"))
    if not fs: continue
    f = pd.concat([pd.read_csv(x) for x in fs]); r = f["last_funding_rate"].astype(float).to_numpy()
    ann = r.mean() * 3 * 365; roll = pd.Series(r).rolling(270).sum() * (365 / 90)
    out.append(f"| {sy} | {100*r.mean():.4f}% | {100*ann:.1f}% | {100*roll.max():.0f}% | {100*roll.min():.0f}% | {100*(r>0).mean():.0f}% |")
out.append("\nEven the best 90-day windows are a small fraction of +61% per month, and the income is only collected while accepting full price risk (or a hedge that doubles costs and needs a spot venue BTCC may not offer).")
Path("docs/OUTLOOK.md").write_text("\n".join(out) + "\n"); print("\n".join(out))
