"""Refinery: take the best development-stage performers, explain where/why their edge appears and disappears, then improve them by trial and error.

Honest design (the failure it targets is dev->out-of-sample decay, not a lack of tweaking):
 * Everything runs on DEVELOPMENT data only (train+validation).  The locked test is never read; refined children still go through the full funnel,
   which consumes its own one-shot locked test like any other strategy.
 * Autopsy: per-window net edge (8 consecutive dev windows), long vs short, cost share -> is the edge broad or concentrated in one regime?
 * Ratchet: windows alternate "propose" (odd) / "confirm" (even).  An edit is accepted only if it improves the proposing windows AND does not hurt
   the confirming windows AND does not reduce the share of positive windows.  A tweak that only fits noise rarely satisfies all three.
 * Edit lineage and per-edit-type outcomes are stored, so the system learns which kinds of change actually generalise (refine_insights)."""
from __future__ import annotations
import copy, json, time
import numpy as np
from . import strategies as ST, validation as V
from .backtest import metrics

K_WIN = 8; MIN_N = 8; WARM = 600


def windows(F, hi, k=K_WIN):
    edges = np.linspace(WARM, hi, k + 1).astype(int)
    return edges, F.epoch_s[np.minimum(edges, F.n - 1)]


def profile(F, spec, em, hi, cache=None):
    """Per-window edge on development data. Returns dict(R=[k], n=[k], gross=[k], long=..., short=...)."""
    tr = V.run(F, spec, em, WARM, hi, cache)
    _, te = windows(F, hi)
    R, N, G = [], [], []
    for a, b in zip(te[:-1], te[1:]):
        w = tr.slice_time(a, b)
        N.append(len(w)); R.append(float(w.R.mean()) if len(w) >= MIN_N else 0.0)
        G.append(float(V.gross_R(w)) if len(w) >= MIN_N else 0.0)
    d = tr.dir; lg = tr.R[d > 0] if len(tr) else np.array([]); sh = tr.R[d < 0] if len(tr) else np.array([])
    return dict(R=np.array(R), n=np.array(N), gross=np.array(G), trades=len(tr), long_R=float(lg.mean()) if len(lg) > 10 else None,
                short_R=float(sh.mean()) if len(sh) > 10 else None, long_n=int(len(lg)), short_n=int(len(sh)))


def score(R, n, idx):
    """Consistency objective over a subset of windows: mean edge minus dispersion, with a floor on trade counts."""
    r = np.array([R[i] if n[i] >= MIN_N else -0.05 for i in idx])
    return float(r.mean() - 0.5 * r.std()), float((r > 0).mean())


def down_R(F, p, hi):
    """Mean net edge over dev windows in which the market did not rise > +5% (None if there are none)."""
    edges, _ = windows(F, hi); num = den = 0.0
    for i in range(K_WIN):
        if F.c[min(edges[i + 1], F.n) - 1] / F.c[edges[i]] - 1 <= 0.05 and p["n"][i] >= MIN_N: num += p["n"][i] * p["R"][i]; den += p["n"][i]
    return float(num / den) if den else None


def evaluate(p, F=None, hi=None):
    odd, even = range(1, K_WIN, 2), range(0, K_WIN, 2)
    so, po = score(p["R"], p["n"], odd); se, pe = score(p["R"], p["n"], even); sa, pa = score(p["R"], p["n"], range(K_WIN))
    return dict(propose=so, confirm=se, all=sa, pos_odd=po, pos_even=pe, pos_all=pa, trades=p["trades"], down=(down_R(F, p, hi) if F is not None else None))


def accept(parent, child, delta=0.01):
    """Ratchet: improve where we looked, do not degrade where we did not look, do not lose consistency, keep enough trades."""
    return (child["propose"] >= parent["propose"] + delta and child["confirm"] >= parent["confirm"] - 0.003
            and child["pos_all"] >= parent["pos_all"] - 1e-9 and child["trades"] >= 0.6 * parent["trades"]
            and (child.get("down") is None or parent.get("down") is None or child["down"] >= parent["down"] - 0.003))      # never trade away flat/down-market performance


def edits(spec, rng, max_n=18):
    """Candidate single edits, each tagged with a kind (used for learning which kinds generalise)."""
    out = []
    fam = ST.FAMILIES[spec["family"]]
    for s in ST.neighbors(spec, max_n=14, rng=rng):
        kind = "param"
        for k in ("stop_atr", "target_rr", "max_hold"):
            if s.get(k) != spec.get(k): kind = "exit_" + k
        out.append((kind, s))
    for side in ("long", "short"):
        if spec.get("side", "both") == "both":
            s = copy.deepcopy(spec); s["side"] = side; out.append(("side_" + side, s))
    fl = list(spec.get("filters") or [])
    for i in range(len(fl)):
        s = copy.deepcopy(spec); s["filters"] = fl[:i] + fl[i + 1:]; out.append(("drop_filter", s))
    if len(fl) < 2:
        t = str(rng.choice(sorted(ST.FILTER_SPACE)))
        f = {"type": t, **{k: g[int(rng.integers(len(g)))] for k, g in ST.FILTER_SPACE[t].items()}}
        if not (t == "atr_pct" and f["lo"] >= f["hi"]) and not any(x["type"] == t for x in fl):
            s = copy.deepcopy(spec); s["filters"] = fl + [f]; out.append(("add_filter:" + t, s))
    good = [(k, s) for k, s in out if not ST.validate_spec(s)]
    idx = rng.permutation(len(good))[:max_n]
    return [good[i] for i in idx]


def autopsy(F, spec, em, hi):
    p = profile(F, spec, em, hi); e = evaluate(p, F, hi)
    return dict(window_R=[round(float(x), 3) for x in p["R"]], window_n=[int(x) for x in p["n"]], gross_R=[round(float(x), 3) for x in p["gross"]],
                long_R=p["long_R"], short_R=p["short_R"], long_n=p["long_n"], short_n=p["short_n"], score=e,
                verdict=("broad" if e["pos_all"] >= 0.75 else "concentrated" if e["pos_all"] >= 0.5 else "fragile"))


def refine_seed(db, st, store, em, seed, symbol, tf, rounds=5, rng=None, budget_s=90, hints=()):
    """Greedy ratcheted trial-and-error from one seed. Returns (best_spec, lineage list). Never reads the locked test."""
    rng = rng or np.random.default_rng(int(time.time()) % 2**31)
    it = store.get(symbol, tf); F = it["F"]; hi = it["bounds"]["val"][1]; t0 = time.time()
    cache = {}
    cur = copy.deepcopy(seed); pe = evaluate(profile(F, cur, em, hi, cache), F, hi); lineage = []; seen = {ST.spec_hash(cur)}
    for r in range(rounds):
        improved = False
        cand = edits(cur, rng)
        if hints: cand.sort(key=lambda kc: 0 if any(kc[0].startswith(h.split(':')[0]) or h.startswith(kc[0].split(':')[0]) for h in hints) else 1)      # forensics-supported edit kinds first
        for kind, child in cand:
            if time.time() - t0 > budget_s: break
            h = ST.spec_hash(child)
            if h in seen: continue
            seen.add(h)
            try: ce = evaluate(profile(F, child, em, hi, cache), F, hi)
            except Exception: continue
            ok = accept(pe, ce)
            lineage.append(dict(round=r, kind=kind, ok=bool(ok), d_propose=round(ce["propose"] - pe["propose"], 4), d_confirm=round(ce["confirm"] - pe["confirm"], 4),
                                pos=round(ce["pos_all"], 3), spec=child if ok else None))
            if ok: cur, pe, improved = child, ce, True; break          # take the first ratchet-passing edit, then re-explore around it
        if not improved or time.time() - t0 > budget_s: break
    return cur, pe, lineage


def pick_seeds(db, per_cell=3, days=3):
    rows = db.q("""SELECT s.id sid, s.spec_json sj, e.symbol sy, e.timeframe tf, e.metrics_json m FROM experiments e JOIN strategies s ON s.id=e.strategy_id
                   WHERE e.stage='validate' AND e.status='pass' AND e.synthetic=0 AND e.timeframe!='4h' AND e.finished>? ORDER BY e.id DESC LIMIT 4000""", (time.time() - days * 86400,))
    done = set(db.get("refined_seeds") or []); best = {}
    for r in rows:
        key = f"{r['sid']}|{r['sy']}|{r['tf']}"
        if key in done: continue
        try:
            m = json.loads(r["m"]); v = m.get("val") or {}; t = float(v.get("t", 0)); w = (m.get("wfo") or {}).get("t", 0)
        except Exception: continue
        best.setdefault((r["sy"], r["tf"]), []).append((t + 0.5 * float(w or 0), key, r["sid"], json.loads(r["sj"])))
    out = []
    for cell, L in best.items():
        for _, key, sid, spec in sorted(L, key=lambda x: -x[0])[:per_cell]: out.append((cell[0], cell[1], key, spec))
    return out


def run_batch(db, st, store, em, max_seeds=6, budget_s=240):
    """One refinery pass: autopsy + ratcheted refinement per seed; accepted children are queued through the normal funnel; insights are updated."""
    t0 = time.time(); rng = np.random.default_rng(int(t0) % 2**31); done = list(db.get("refined_seeds") or []); ins = db.get("refine_insights") or {}
    queued = tried = 0; seeds = pick_seeds(db); rng.shuffle(seeds)
    for sy, tf, key, spec in seeds[:max_seeds]:
        if time.time() - t0 > budget_s: break
        try:
            F = store.get(sy, tf)["F"]; hi = store.get(sy, tf)["bounds"]["val"][1]
            a = autopsy(F, spec, em, hi)
            from . import forensics as FO
            dg = FO.diagnose(db, store, em, key.split("|")[0], spec, sy, tf); a["diagnosis"] = dg["causes"]
            if dg["causes"][0][0] == "no_predictive_edge":        # repair budget: no testable reason to believe an edit helps -> archive instead of tweaking noise
                done.append(key); db.x("INSERT INTO refine_runs(ts,seed,symbol,timeframe,autopsy_json,lineage_json,final_json) VALUES(?,?,?,?,?,?,?)",
                                       (time.time(), key, sy, tf, json.dumps(a), "[]", json.dumps(dict(archived="no_predictive_edge")))); continue
            best, pe, lin = refine_seed(db, st, store, em, spec, sy, tf, rng=rng, budget_s=max(20, budget_s / max_seeds), hints=dg["hints"])
        except Exception as e:
            db.log("warn", "refine", f"seed {key}: {e}"); done.append(key); continue
        done.append(key); tried += len(lin)
        for l in lin:
            d = ins.setdefault(l["kind"], dict(n=0, ok=0, dprop=0.0, dconf=0.0)); d["n"] += 1; d["ok"] += int(l["ok"]); d["dprop"] += l["d_propose"]; d["dconf"] += l["d_confirm"]
        db.x("INSERT INTO refine_runs(ts,seed,symbol,timeframe,autopsy_json,lineage_json,final_json) VALUES(?,?,?,?,?,?,?)",
             (time.time(), key, sy, tf, json.dumps(a), json.dumps(lin), json.dumps(dict(spec=best, eval=pe))))
        if ST.spec_hash(best) != ST.spec_hash(spec):
            db.enqueue("experiment", dict(spec=best, symbol=sy, timeframe=tf, note=f"refine:{key[:12]}"[:120]), priority=3); queued += 1
    db.set("refined_seeds", done[-3000:]); db.set("refine_insights", ins); db.set("refine_last", time.time())
    return dict(seeds=len(seeds[:max_seeds]), tried=tried, queued=queued)
