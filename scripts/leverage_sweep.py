"""Leverage scenario sweep (Agent G): for the best dev-validated candidates, evaluate the $300 account across leverage 5..500x and risk fractions,
under the configured ExchangeModel, using ONLY development-period trades (train+validation). Reports where leverage helps or hurts after liquidation/ruin.
Usage: python scripts/leverage_sweep.py [n_candidates=6] [preset]"""
import sys, json
import numpy as np
from dataclasses import replace
from strategylab.db import DB
from strategylab.config import Settings
from strategylab.engine import DataStore
from strategylab.worker import make_loader
from strategylab import btcc, capital as CP, validation as V, refine as RF

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6; preset = sys.argv[2] if len(sys.argv) > 2 else "conservative"
db = DB(); st = Settings.load(); store = DataStore(st, make_loader()); em0 = btcc.get_model(preset)
LEVS = [5, 10, 25, 50, 100, 250, 500]; RISKS = [0.02, 0.05, 0.10, 0.20, 0.50]
lines = [f"# Leverage sweep ({preset}; dev-period trades only; $300 start; ruin = equity<=floor)\n", "| candidate | lev | best risk | P($1k/mo) | ruin | lose50 | median wealth | liq-paths | verdict |", "|---|---|---|---|---|---|---|---|---|"]
for sy, tf, key, spec in RF.pick_seeds(db, per_cell=2, days=30)[:n]:
    it = store.get(sy, tf); F = it["F"]; hi = it["bounds"]["val"][1]
    for L in LEVS:
        if L > em0.max_leverage: continue
        em = replace(em0, max_leverage=max(em0.max_leverage, L))
        tr = V.run(F, spec, em, RF.WARM, hi, None)
        if len(tr) < 30: lines.append(f"| {spec['family']} {sy} {tf} | {L}x | - | - | - | - | - | - | only {len(tr)} tradable trades |"); continue
        t0, t1 = float(F.epoch_s[RF.WARM]), float(F.epoch_s[hi - 1]); blocks = CP.month_blocks(tr, t0, t1); rng = np.random.default_rng(3); idx = rng.integers(0, len(blocks), size=(500, 6))
        best = None
        for r in RISKS:
            s = CP.summarize(CP.simulate(blocks, idx, sy, r, L, em), 300.0, 1000.0)
            row = (s["ruin"]["p"] <= 0.5, s["monthly_withdraw_any"]["1000"]["p"], s["final_wealth_pct"]["50"], r, s)
            if best is None or row[:3] > best[:3]: best = row
        _, p1k, med, r, s = best
        v = "viable" if s["ruin"]["p"] <= 0.5 and s["lose_50pct"]["p"] <= 0.95 and med > 300 else "ruin/negative"
        lines.append(f"| {spec['family']} {sy} {tf} stop{spec['stop_atr']} | {L}x | {r} | {p1k:.2%} | {s['ruin']['p']:.1%} | {s['lose_50pct']['p']:.1%} | ${med:,.0f} | {s['any_liquidation']['p']:.0%} | {v} |")
open("docs/LEVERAGE_SWEEP.md", "w").write("\n".join(lines) + "\n"); print("\n".join(lines))
