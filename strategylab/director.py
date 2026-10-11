"""Adaptive research director: decides WHAT to test next, and why.

Allocation signal = development-data outcomes only (Stage A pass, screen executed, validation PASS). Locked-test results are never
used as feedback. Arms are (family, market, timeframe); reward is validation-pass rate among hypotheses that cleared Stage A,
weighted by feasibility and by compute cost (information per CPU-second), sampled with Thompson sampling over Beta posteriors.
Exploration budget is always reserved; stagnation (no improvement in the best dev-validated score for N experiments) switches the
director into a different direction: dominant arms are cooled down, exploration share rises, underexplored feasible cells and
GP search get the budget.  Every batch records its composition and a plain-language reason."""
from __future__ import annotations
import copy, json, math, time
import numpy as np
from . import strategies as ST, feasibility as FE, grammar as G
from .btcc import get_model
from .agents import Memory, ResearchScientist, StrategyInventor

STAG_EXPERIMENTS = 4000        # experiments without an improvement in the best validated score => stagnation
STAG_MODE_LEN = 1500           # experiments spent in the new direction before returning to normal allocation
SHARES = {"normal": (0.55, 0.25, 0.20), "stagnation": (0.20, 0.30, 0.50)}      # exploit, evolve, explore
TF_PRIOR = {"1m": 0.6, "5m": 0.9, "15m": 0.45, "1h": 0.0}                       # tie-breaking priority only (task spec); 4h excluded from routine research


class Director:
    def __init__(self, db, st, store, em=None):
        self.db, self.st, self.store = db, st, store; self.em = em or get_model(st.exchange_preset)
        self.mem = Memory(db); self.sci = ResearchScientist(db, None); self.inv = StrategyInventor(db, self.mem)
        self._atoms = {}; self._stops = {}

    # ------------------------------------------------------------------ evidence
    def dead_cells(self):
        """Self-heal: (symbol, tf) cells whose recent screens show no gross edge are dropped from exploitation/exploration (re-probed ~hourly with fresh data)."""
        now = time.time(); c = self.db.get("dead_cells") or {}
        if now - c.get("ts", 0) < 1200: return {tuple(k.split("|")) for k in c.get("cells", [])}
        dead = []
        for sy in self.st.symbols:
            for tf in self.st.timeframes:
                ms = [json.loads(r["metrics_json"]) for r in self.db.q("SELECT metrics_json FROM experiments WHERE stage='screen' AND synthetic=0 AND symbol=? AND timeframe=? ORDER BY id DESC LIMIT 600", (sy, tf))]
                ms = [m for m in ms if m.get("gross_mean_R") is not None and m.get("avg_stop")]
                if len(ms) >= 400 and sum(m["mean_R"] > 0.05 for m in ms) / len(ms) < 0.01 and sum(m["gross_mean_R"] > 0 for m in ms) / len(ms) < 0.10:
                    dead.append(f"{sy}|{tf}")
        if set(dead) != set(c.get("cells", [])): self.db.log("info", "director", f"self-heal: cells with no gross edge suppressed: {dead or 'none'}")
        self.db.set("dead_cells", dict(ts=now, cells=dead)); return {tuple(k.split("|")) for k in dead}

    def arms(self):
        dead = self.dead_cells(); cells = [c for c in self.store.available() if c not in dead] or self.store.available(); out = {}
        reg = {(r["family"], r["symbol"], r["timeframe"]): r for r in self.db.q(
            """SELECT family, symbol, timeframe, COUNT(*) n, SUM(best_stage>=1) screened, SUM(best_stage>=3) vpass, SUM(n_variants) variants
               FROM hyp_registry GROUP BY 1,2,3""")}
        secs = self._secs_per_hypothesis()
        for fam in ST.FAMILIES:
            for (sy, tf) in cells:
                r = reg.get((fam, sy, tf), dict(n=0, screened=0, vpass=0, variants=0))
                out[(fam, sy, tf)] = dict(n=r["n"], screened=r["screened"] or 0, vpass=r["vpass"] or 0, secs=secs.get((fam, tf), 0.3))
        return out

    def _secs_per_hypothesis(self):
        rows = self.db.q("""SELECT s.family f, e.timeframe tf, AVG(e.t1-e.t0) a FROM (
                              SELECT strategy_id, symbol, timeframe, MIN(started) t0, MAX(finished) t1 FROM experiments
                              WHERE id > (SELECT COALESCE(MAX(id),0)-8000 FROM experiments) AND stage IN ('eligibility','screen','validate','final') GROUP BY 1,2,3) e
                            JOIN strategies s ON s.id=e.strategy_id GROUP BY 1,2""")
        return {(r["f"], r["tf"]): max(r["a"] or 0.1, 0.02) for r in rows}

    def regime_hint(self, fam, sy, tf):
        rows = self.db.q("SELECT reasons FROM lessons WHERE family=? AND symbol=? AND timeframe=? ORDER BY id DESC LIMIT 120", (fam, sy, tf)); n = bad = 0
        for r in rows:
            rs = json.loads(r["reasons"])
            if any(x in ("fails_out_of_sample", "regime_dependent_or_unstable", "single_regime_dependence", "frequency_single_period") for x in rs):
                n += 1; bad += any(x in ("regime_dependent_or_unstable", "single_regime_dependence", "frequency_single_period") for x in rs)
        return n >= 10 and bad / n > 0.35

    # ------------------------------------------------------------------ stagnation
    def _state(self): return self.db.get("director_state", dict(mode="normal", direction=0, exp_last_improve=0, best=0.0, hyp_prev=0, exp_last_newhyp=0, mode_since=0, cooldown={}, scan_ts=0.0))

    def _update_progress(self, s):
        exp = self.db.q("SELECT COUNT(*) n FROM experiments")[0]["n"]; hyp = self.db.q("SELECT COUNT(*) n FROM hyp_registry")[0]["n"]
        if hyp > s["hyp_prev"]: s["exp_last_newhyp"] = exp
        s["hyp_prev"] = hyp
        best = s["best"]
        for r in self.db.q("""SELECT ss.summary_json sj, ss.updated u FROM strategy_status ss WHERE ss.synthetic=0 AND ss.updated>? AND ss.symbol!='MULTI'""", (s["scan_ts"],)):
            try: sm = json.loads(r["sj"]); v = sm["validate"]["val"]; el = sm.get("eligibility", {})
            except Exception: continue
            if "final" not in sm or el.get("decision", "pass") != "pass" or v.get("mean_R", 0) <= 0: continue      # validated on dev data, Stage-A feasible
            best = max(best, float(v.get("t", 0)))
        s["scan_ts"] = time.time()
        if best > s["best"] * 1.02 + 1e-9: s["exp_last_improve"] = exp
        s["best"] = best; s["exp"] = exp
        return s

    def _mode(self, s):
        exp = s["exp"]
        if s["mode"] == "normal" and exp - s["exp_last_improve"] > STAG_EXPERIMENTS:
            s.update(mode="stagnation", mode_since=exp, direction=s["direction"] + 1)
            arms = self.arms(); top = sorted(arms.items(), key=lambda kv: -kv[1]["n"])[:4]       # most-sampled arms get a cooldown
            s["cooldown"] = {"|".join(k): exp + STAG_MODE_LEN for k, _ in top}
            self.db.log("warn", "director", f"STAGNATION: {exp - s['exp_last_improve']} experiments without improvement (best dev-validated t={s['best']:.2f}); cooling {list(s['cooldown'])}; direction #{s['direction']}")
        elif s["mode"] == "stagnation" and (exp - s["mode_since"] > STAG_MODE_LEN or s["exp_last_improve"] > s["mode_since"]):
            s.update(mode="normal", cooldown={}, exp_last_improve=exp)
        return s["mode"]

    # ------------------------------------------------------------------ generation
    def _stops_ok(self, sy, tf):
        k = (sy, tf)
        if k not in self._stops:
            it = self.store.get(sy, tf); fake = dict(atr_n=14)
            self._stops[k] = [x for x in ST.EXIT_SPACE["stop_atr"] if FE.precheck_cost(it["F"], dict(fake, stop_atr=x), it["bounds"], self.em)["ok"]]
        return self._stops[k]

    def _atom_weights(self, sy, tf, side):
        k = (sy, tf)
        if k not in self._atoms:
            it = self.store.get(sy, tf); lo, hi = it["bounds"]["train"]
            self._atoms[k] = G.atom_screen(it["F"], lo, hi)
        t = self._atoms[k]; return {n: (max(v, 0) if side == "long" else max(-v, 0)) for n, v in t.items()}

    def _exits(self, rng, sy, tf):
        ok = self._stops_ok(sy, tf)
        if not ok: return None
        return dict(stop_atr=float(ok[int(rng.integers(len(ok)))]), target_rr=float(rng.choice(ST.EXIT_SPACE["target_rr"])), max_hold=int(rng.choice(ST.EXIT_SPACE["max_hold"])), atr_n=14)

    def _gp_candidate(self, rng, sy, tf, evolve):
        it = self.store.get(sy, tf); meta = it["meta"]
        avail = lambda n: G.available(n, True, bool(meta.get("has_funding")), it["F"].ref is not None)
        side = str(rng.choice(["long", "short"])); w = self._atom_weights(sy, tf, side); rule = None; how = "random"
        if evolve:
            el = self._gp_elites(sy, tf)
            if len(el) >= 2 and rng.random() < 0.5:
                a, b = el[int(rng.integers(len(el)))], el[int(rng.integers(len(el)))]; side = a[1]; rule = G.crossover(a[0], b[0], rng) if a[1] == b[1] else G.mutate(a[0], rng, w, avail); how = "crossover"
            elif el:
                a = el[int(rng.integers(len(el)))]; side = a[1]; rule = G.mutate(a[0], rng, w, avail); how = "mutation"
        if rule is None: rule = G.random_rule(rng, side, w, None, avail)
        if G.validate_rule(rule, side): return None
        ex = self._exits(rng, sy, tf)
        if ex is None: return None
        spec = dict(family="gp_rule", params=dict(rule=rule, side=side), side=side, filters=[], **ex)
        return spec, f"gp:{how} {G.describe(rule, side)}"

    def _gp_elites(self, sy, tf, cap_per_niche=2, k=24):
        rows = self.db.q("""SELECT s.spec_json sp, ss.summary_json sj FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id
                            WHERE s.family='gp_rule' AND ss.symbol=? AND ss.timeframe=? AND ss.synthetic=0 ORDER BY ss.updated DESC LIMIT 400""", (sy, tf))
        scored = []
        for r in rows:
            try: sm = json.loads(r["sj"]); sp = json.loads(r["sp"])
            except Exception: continue
            if "screen" not in sm: continue
            fit = sm["screen"].get("t", 0) + (3 if "final" in sm else 1 if "validate" in sm else 0)          # dev-data fitness only (screen t + validation reached)
            scored.append((fit, sp["params"]["rule"], sp["params"]["side"]))
        scored.sort(key=lambda x: -x[0]); out, per = [], {}
        for fit, rule, side in scored:
            n = G.rule_key(rule) + side
            if per.get(n, 0) >= cap_per_niche: continue                  # diversity preservation: cap elites per feature-set niche
            per[n] = per.get(n, 0) + 1; out.append((rule, side))
            if len(out) >= k: break
        return out

    def _classic_candidate(self, rng, fam, sy, tf, evolve, regime):
        it = self.store.get(sy, tf)
        if evolve:
            rows = self.db.q("""SELECT params_json, side FROM hyp_registry WHERE family=? AND symbol=? AND timeframe=? AND best_stage>=3 ORDER BY last_ts DESC LIMIT 30""", (fam, sy, tf))
            if rows:
                r = rows[int(rng.integers(len(rows)))]; params = json.loads(r["params_json"]); grid = ST.FAMILIES[fam]["space"]
                ks = [k for k, g in grid.items() if g and k in params]
                if ks:
                    k = ks[int(rng.integers(len(ks)))]; g = grid[k]; j = g.index(params[k]) if params[k] in g else 0
                    params[k] = g[min(max(j + int(rng.choice([-2, -1, 1, 2])), 0), len(g) - 1)]
                spec = dict(family=fam, params=params, side=str(r["side"] or "both"), filters=[]); how = "evolve"
            else: evolve = False
        if not evolve:
            spec = self.inv.invent(fam, sy, tf, rng, it); how = "explore"
        ex = self._exits(rng, sy, tf)
        if ex is None: return None
        spec.update(ex); spec["filters"] = list(spec.get("filters") or [])
        if regime and rng.random() < 0.6:
            t = str(rng.choice(["er_regime", "atr_pct", "htf_trend"]))
            f = {"type": t, **{k: g[int(rng.integers(len(g)))] for k, g in ST.FILTER_SPACE[t].items()}}
            if t == "atr_pct" and f["lo"] >= f["hi"]: f["hi"] = 1.0
            spec["filters"] = [x for x in spec["filters"] if x["type"] != t] + [f]; how += "+regime-filter"
        return spec, f"{how}"

    # ------------------------------------------------------------------ batch
    def plan_batch(self, n=24, seed=None):
        self.sci.seed_catalog()
        s = self._update_progress(self._state()); mode = self._mode(s); exp = s["exp"]
        rng = np.random.default_rng(seed if seed is not None else 7000 + self.db.get("director_counter", 0)); self.db.set("director_counter", self.db.get("director_counter", 0) + 1)
        arms = self.arms(); cool = {k for k, until in s["cooldown"].items() if until > exp}
        # Thompson sample: P(stage A pass) x P(validation pass | screened), per compute second; infeasible/cooled arms excluded
        def draw(k, a):
            if "|".join(k) in cool: return -1
            ta = rng.beta(1 + a["screened"], 1 + max(a["n"] - a["screened"], 0)); tv = rng.beta(1 + a["vpass"], 1 + max(a["screened"] - a["vpass"], 0))
            return ta * tv / (a["secs"] + 0.05) + 0.02 * TF_PRIOR.get(k[2], 0)
        ranked = sorted(arms.items(), key=lambda kv: -draw(*kv)); n_ex, n_ev, n_xp = [round(n * f) for f in SHARES[mode]]
        jobs, comp, tried = [], dict(exploit=0, evolve=0, explore=0, rejected_precheck=0), 0
        under = sorted([kv for kv in arms.items() if "|".join(kv[0]) not in cool and kv[1]["n"] < 40], key=lambda kv: (kv[1]["n"], rng.random()))
        top = ranked[:max(4, len(ranked) // 8)]

        def attempt(kind, arm):
            nonlocal tried
            tried += 1; fam, sy, tf = arm[0]
            regime = self.regime_hint(fam, sy, tf) if kind != "explore" else False
            cand = self._gp_candidate(rng, sy, tf, kind == "evolve") if fam == "gp_rule" else self._classic_candidate(rng, fam, sy, tf, kind == "evolve", regime)
            if not cand: return False
            spec, how = cand
            if ST.validate_spec(spec): return False
            it = self.store.get(sy, tf); pc = FE.precheck_cost(it["F"], spec, it["bounds"], self.em); ok, why = FE.novelty_check(self.db, spec, sy, tf)
            h = ST.spec_hash(spec)
            if not pc["ok"] or not ok or any(j["_h"] == h and j["symbol"] == sy and j["timeframe"] == tf for j in jobs) or self.mem.seen(spec, sy, tf):
                comp["rejected_precheck"] += 1
                reason = "cost_infeasible_precheck" if not pc["ok"] else (why or "duplicate")
                self.db.x("INSERT INTO precheck_rejections(ts,family,symbol,timeframe,reason,hkey) VALUES(?,?,?,?,?,?)", (time.time(), fam, sy, tf, reason, FE.hyp_keys(spec, sy, tf)[1])); return False
            jobs.append(dict(spec=spec, symbol=sy, timeframe=tf, note=f"director:{kind}:{how}"[:120], hypothesis_id=self.sci.hypothesis_id(fam), _h=h)); comp[kind] += 1; return True

        for kind, quota, pool in (("exploit", n_ex, top), ("evolve", n_ev, top), ("explore", n_xp, under or ranked)):
            guard = 0
            while comp[kind] < quota and guard < quota * 25:
                guard += 1; arm = pool[int(rng.integers(len(pool)))]; attempt(kind, arm)
        why = self._explain(mode, s, comp, top, under, cool, tried)
        self.db.x("INSERT INTO director_batches(ts,mode,n,composition_json,reason) VALUES(?,?,?,?,?)", (time.time(), mode, len(jobs), json.dumps(comp), why))
        self.db.set("director_state", s); self.db.set("director_last", dict(mode=mode, reason=why, comp=comp, ts=time.time(), best=s["best"], since_improve=exp - s["exp_last_improve"], since_newhyp=exp - s["exp_last_newhyp"]))
        self.db.x("INSERT INTO research_metrics(ts,body) VALUES(?,?)", (time.time(), json.dumps(dict(exp=exp, hyp=s["hyp_prev"], best=s["best"], since_improve=exp - s["exp_last_improve"], mode=mode, rejected_precheck=comp["rejected_precheck"]))))
        return jobs

    def _explain(self, mode, s, comp, top, under, cool, tried):
        names = lambda arms: ", ".join(f"{k[0]}/{k[1][:3]}/{k[2]}" for k, _ in arms[:3])
        w = f"[{mode}] exploit {comp['exploit']} on top Thompson arms ({names(top)}); evolve {comp['evolve']} from dev-validated elites/GP; explore {comp['explore']} in least-tested feasible cells ({names(under) or 'n/a'}); " \
            f"{comp['rejected_precheck']} candidates dropped before queueing (cost/novelty) out of {tried} generated. Best dev-validated t={s['best']:.2f}, {s['exp'] - s['exp_last_improve']} experiments since improvement, {s['exp'] - s['exp_last_newhyp']} since a new hypothesis."
        if cool: w += f" Cooling down: {', '.join(cool)}."
        return w

    def status(self):
        d = self.db.get("director_last", {}); s = self._state()
        return dict(mode=s["mode"], direction=s["direction"], last=d, best=s["best"], since_improve=(s.get("exp", 0) - s["exp_last_improve"]) if s.get("exp") else None)
