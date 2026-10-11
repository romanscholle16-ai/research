"""Cooperating research agents (software modules sharing one AI router).

 Research Scientist  : hypothesis catalog + (optional) AI-proposed novel hypotheses; records data-blocked ideas honestly.
 Strategy Inventor   : turns a hypothesis into a concrete deterministic spec (parameters, exits, sides, fitted-on-train items).
 Evolution Specialist: studies failures/near-misses, proposes meaningful variations (never re-tests the same spec).
 Research Supervisor : bandit-style experiment selection across families / instruments / timeframes, queue management.
 (Backtesting Engineer, Statistical Validator, Risk Specialist live in engine.py / validation.py / capital.py.)
"""
from __future__ import annotations
import copy, json, math, re, time
import numpy as np
from . import strategies as ST

DEPTH = {"REJECTED": 0, "PROMISING": 3, "BACKTEST_QUALIFIED": 4, "PAPER_VALIDATED": 5, "LIVE_ELIGIBLE": 6}


class NoData(Exception):
    pass


class Memory:
    def __init__(self, db): self.db = db

    def seen(self, spec, symbol, tf):
        sid = "S-" + ST.spec_hash(spec)[:10]
        if self.db.q("SELECT 1 FROM strategy_status WHERE strategy_id=? AND symbol=? AND timeframe=?", (sid, symbol, tf)): return True
        # also dedupe against queued/running jobs
        h = ST.spec_hash(spec)
        return bool(self.db.q("SELECT 1 FROM jobs WHERE kind='experiment' AND status IN ('queued','running') AND payload LIKE ? AND payload LIKE ? AND payload LIKE ?",
                              (f'%"_h": "{h}"%', f'%"symbol": "{symbol}"%', f'%"timeframe": "{tf}"%')))

    def cell_stats(self):
        """Per (family,symbol,tf): n tested and mean depth reached (reject at screen=0 ... promising=3)."""
        rows = self.db.q("""SELECT s.family f, ss.symbol sy, ss.timeframe tf, ss.status st, ss.summary_json sj
                            FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id""")
        out = {}
        for r in rows:
            d = DEPTH.get(r["st"], 0)
            if r["st"] == "REJECTED":
                try:
                    sm = json.loads(r["sj"]); d = 2 if "validate" in sm else 1 if (sm.get("screen") and not [x for x in sm.get("reasons", []) if x in ("too_few_trades", "no_edge", "cost_dominated", "statistically_insignificant", "poor_risk_reward")]) else 0
                except Exception: d = 0
            k = (r["f"], r["sy"], r["tf"]); a = out.setdefault(k, [0, 0.0]); a[0] += 1; a[1] += d
        return out

    def reason_counts(self, family, symbol=None, tf=None):
        q = "SELECT reasons FROM lessons WHERE family=?"; a = [family]
        if symbol: q += " AND symbol=?"; a.append(symbol)
        if tf: q += " AND timeframe=?"; a.append(tf)
        c = {}
        for r in self.db.q(q + " ORDER BY id DESC LIMIT 200", tuple(a)):
            for x in json.loads(r["reasons"]): c[x] = c.get(x, 0) + 1
        return c


class ResearchScientist:
    def __init__(self, db, router): self.db, self.router = db, router

    def seed_catalog(self):
        for fam, f in ST.FAMILIES.items():
            if not self.db.q("SELECT 1 FROM hypotheses WHERE family=? AND source='catalog'", (fam,)):
                self.db.x("INSERT INTO hypotheses(created,family,text,rationale,falsification,source) VALUES(?,?,?,?,?,'catalog')",
                          (time.time(), fam, f["text"], f["why"], f["falsify"]))
        for name, why in ST.BLOCKED_HYPOTHESES.items():
            if not self.db.q("SELECT 1 FROM hypotheses WHERE family=?", (name,)):
                self.db.x("INSERT INTO hypotheses(created,family,text,rationale,falsification,source,status) VALUES(?,?,?,?,?,'catalog','blocked_data')",
                          (time.time(), name, name.replace("_", " "), why, "n/a until data acquired", ))
                self.db.log("warn", "data", f"hypothesis '{name}' BLOCKED: {why}")

    def hypothesis_id(self, family):
        r = self.db.q("SELECT id FROM hypotheses WHERE family=? ORDER BY id LIMIT 1", (family,))
        return r[0]["id"] if r else None

    def ai_proposals(self, avail_cells, mem: Memory, k=3):
        """Ask the AI for novel, falsifiable variants expressed in the strategy DSL. Output strictly validated."""
        if not self.router: return []
        fams = {f: v["space"] for f, v in ST.FAMILIES.items() if f != "session_drift"}
        recent = self.db.q("SELECT family, reasons FROM lessons ORDER BY id DESC LIMIT 15")
        sys_p = "You are a quantitative crypto-futures researcher. Reply with ONLY a JSON list. No prose."
        prompt = (f"Propose up to {k} NOVEL, falsifiable intraday/swing strategy variants for BTCUSDT/ETHUSDT perpetuals using ONLY these families and parameter grids:\n"
                  f"{json.dumps(fams)}\nExit params: stop_atr in [1,1.5,2,3], target_rr in [0,1,1.5,2,3], max_hold in [12,24,48,96], side in long/short/both. "
                  f"Optional filters list items: {{'type':'ema_trend','n':100}} or {{'type':'atr_pct','w':500,'lo':0.2,'hi':1.0}}.\n"
                  f"Recent failures: {json.dumps(recent)[:900]}\nAvoid repeating failures. Each item: "
                  f'{{"family":..,"params":{{..}},"side":..,"stop_atr":..,"target_rr":..,"max_hold":..,"filters":[],"timeframe":"15m|1h|4h","symbol":"BTCUSDT|ETHUSDT","hypothesis":"why an edge may exist","falsification":"test that would refute it"}}')
        r = self.router.ask(sys_p, prompt, 1200)
        if not r: return []
        out = []
        try:
            m = re.search(r"\[.*\]", r.text, re.S); items = json.loads(m.group(0)) if m else []
        except Exception: self.db.log("warn", "ai", "unparseable AI proposal discarded"); return []
        for it in items[:k]:
            if not isinstance(it, dict): continue
            it.setdefault("atr_n", 14); it.setdefault("filters", [])
            errs = ST.validate_spec(it)
            fl_ok = all(isinstance(f, dict) and f.get("type") in ST.FILTER_SPACE for f in it["filters"])
            if errs or not fl_ok or (it.get("symbol"), it.get("timeframe")) not in avail_cells:
                self.db.log("info", "ai", f"AI proposal rejected by validator: {errs or 'bad filter/cell'}"); continue
            out.append(it)
        return out


class StrategyInventor:
    def __init__(self, db, mem: Memory): self.db, self.mem = db, mem

    def _pick(self, rng, grid, prefer=None):
        if prefer:
            cand = [g for g in grid if g in prefer]
            if cand and rng.random() < 0.7: return cand[int(rng.integers(len(cand)))]
        return grid[int(rng.integers(len(grid)))]

    def invent(self, family, symbol, tf, rng, item=None):
        fam = ST.FAMILIES[family]; rc = self.mem.reason_counts(family, symbol, tf)
        costly = rc.get("cost_dominated", 0) > 3
        params = {}
        for k, grid in fam["space"].items():
            if grid is None: continue
            params[k] = self._pick(rng, grid)
        if family == "session_drift":
            params.update(self._fit_session(item, rng))
        spec = dict(family=family, params=params, side=str(rng.choice(["both", "both", "long", "short"])),
                    stop_atr=self._pick(rng, ST.EXIT_SPACE["stop_atr"], [2.0, 3.0] if costly else None),
                    target_rr=self._pick(rng, ST.EXIT_SPACE["target_rr"], [0, 2.0, 3.0] if costly else None),
                    max_hold=self._pick(rng, ST.EXIT_SPACE["max_hold"], [48, 96] if costly else None), atr_n=14, filters=[])
        return spec

    def _fit_session(self, item, rng):
        """Fit hour-of-day drift on TRAIN bars only (documented selection step)."""
        F, (lo, hi) = item["F"], item["bounds"]["train"]
        h = F.hour[lo:hi - 5]; fwd = (F.c[lo + 4:hi - 1] / F.o[lo + 1:hi - 4] - 1)[: len(h)]
        best = []
        for hr in range(24):
            x = fwd[(h[: len(fwd)] == hr) & (F.minute[lo:hi - 5][: len(fwd)] == 0)]
            if len(x) > 100: best.append((abs(x.mean() / (x.std() / math.sqrt(len(x)) + 1e-12)), hr, 1 if x.mean() > 0 else -1))
        best.sort(reverse=True)
        if not best: return {"hours": [0], "dir": 1}
        _, hr, d = best[int(rng.integers(min(3, len(best))))]
        return {"hours": [hr], "dir": d}


class EvolutionSpecialist:
    def __init__(self, db, mem: Memory): self.db, self.mem = db, mem

    def near_misses(self, limit=20):
        rows = self.db.q("""SELECT ss.strategy_id sid, ss.symbol sy, ss.timeframe tf, ss.summary_json sj, ss.status st, s.spec_json spec
                            FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id
                            WHERE ss.synthetic=(SELECT COALESCE(MAX(synthetic),0) FROM datasets) ORDER BY ss.updated DESC LIMIT 600""")
        scored = []
        for r in rows:
            try: sm = json.loads(r["sj"])
            except Exception: continue
            sc = 0.0
            if "validate" in sm: sc += 2
            if "final" in sm: sc += 3
            m = sm.get("screen", {})
            if m.get("n", 0) >= 30: sc += max(min(m.get("mean_R", 0), 0.5), -0.2) * 4 + max(min(m.get("t", 0), 4), -1)
            if r["st"] == "PROMISING": sc += 3
            if sc > 0.5: scored.append((sc, r))
        scored.sort(key=lambda x: -x[0])
        per, out = {}, []
        for sc, r in scored:                      # diversity: at most 4 parents per family so one family cannot flood the search
            fam = json.loads(r["spec"])["family"]
            if per.get(fam, 0) < 4: per[fam] = per.get(fam, 0) + 1; out.append((sc, r))
        return out[:limit]

    def mutate(self, row, rng):
        spec = json.loads(row["spec"]); sym, tf = row["sy"], row["tf"]; sm = json.loads(row["sj"])
        reasons = sm.get("reasons", []); s = copy.deepcopy(spec); fam = ST.FAMILIES[s["family"]]
        move = rng.choice(["exit", "param", "filter", "side", "instrument", "timeframe"],
                          p=[.25, .2, .25, .1, .1, .1])
        if "unstable_parameters_overfit" in reasons or "fails_walk_forward" in reasons: move = rng.choice(["filter", "instrument", "timeframe"])
        if "cost_dominated" in reasons or "cost_sensitive" in reasons:
            s["stop_atr"] = max(s["stop_atr"], 2.0); s["max_hold"] = max(s["max_hold"], 48); move = "exit_wide"
        if "too_few_trades" in reasons and move == "param": move = "timeframe"
        note = f"evolve:{move} from {row['sid']} ({','.join(reasons)[:60]})"
        if move == "exit": k = rng.choice(["stop_atr", "target_rr", "max_hold"]); s[k] = rng.choice(ST.EXIT_SPACE[k]).item()
        elif move == "param":
            ks = [k for k, g in fam["space"].items() if g]
            if ks: k = ks[int(rng.integers(len(ks)))]; s["params"][k] = fam["space"][k][int(rng.integers(len(fam["space"][k])))]
        elif move == "filter":
            t = str(rng.choice(list(ST.FILTER_SPACE)))
            f = {"type": t, **{k: g[int(rng.integers(len(g)))] for k, g in ST.FILTER_SPACE[t].items()}}
            if t == "atr_pct" and f["lo"] >= f["hi"]: f["hi"] = 1.0
            s["filters"] = [x for x in s.get("filters", []) if x["type"] != t] + [f]
        elif move == "side": s["side"] = str(rng.choice([x for x in ST.SIDES if x != s["side"]]))
        elif move == "instrument": sym = str(rng.choice(["BTCUSDT", "ETHUSDT"]))
        elif move == "timeframe": tf = str(rng.choice(["15m", "1h", "4h"]))
        return s, sym, tf, note


class Supervisor:
    """Chooses the next experiments from memory. Never stops because nothing has worked; shifts to unexplored cells."""
    def __init__(self, db, st, router, store):
        self.db, self.st, self.router, self.store = db, st, router, store
        self.mem = Memory(db); self.sci = ResearchScientist(db, router); self.inv = StrategyInventor(db, self.mem)
        self.evo = EvolutionSpecialist(db, self.mem)

    def maybe_queue_portfolio(self):
        """Rebuild portfolios whenever the pool of validated strategies has grown by >=20 (never more than hourly)."""
        n = self.db.q("SELECT COUNT(*) n FROM trades WHERE symbol!='MULTI'")[0]["n"]
        last = self.db.get("portfolio_pool_n", 0); lt = self.db.get("portfolio_last_ts", 0)
        if n >= max(10, last + 20) and time.time() - lt > 3600:
            self.db.set("portfolio_pool_n", n); self.db.set("portfolio_last_ts", time.time()); self.db.enqueue("portfolio", {}, priority=2)

    def plan(self, n=8, use_ai=True) -> list[dict]:
        self.sci.seed_catalog()
        cells = self.store.available()
        if not cells: raise NoData("no_data_available")
        counter = self.db.get("plan_counter", 0); self.db.set("plan_counter", counter + 1)
        rng = np.random.default_rng(1000 + counter)
        stats = self.mem.cell_stats(); N = sum(v[0] for v in stats.values()) + 1
        jobs = []; seen_now = set()
        fams = [f for f in ST.FAMILIES]
        crowd = {}
        for (f_, s_, t_), (n_, d_) in stats.items(): crowd[f_] = crowd.get(f_, 0) + n_
        def ucb(f, s, tf):
            a = stats.get((f, s, tf), [0, 0.0])
            pen = min(1.5, 0.15 * math.log1p(crowd.get(f, 0) / 50.0))     # crowding penalty keeps the search diverse
            return 5.0 + rng.random() if a[0] == 0 else a[1] / a[0] + 0.6 * math.sqrt(math.log(N + 1) / a[0]) + 0.1 * rng.random() - pen
        near = self.evo.near_misses() if N > 30 else []
        if use_ai:
            try:
                for p in self.sci.ai_proposals(set(cells), self.mem):
                    jobs.append(self._job(p, p["symbol"], p["timeframe"], f"ai:{p.get('hypothesis','')[:80]}", p["family"]))
            except Exception as e: self.db.log("warn", "supervisor", f"AI proposal step failed: {e}")
        attempts = 0
        while len(jobs) < n and attempts < n * 30:
            attempts += 1; r = rng.random()
            if near and r < 0.35:
                sc, row = near[int(rng.integers(len(near)))]
                spec, sym, tf, note = self.evo.mutate(row, rng); fam = spec["family"]
            else:
                scored = sorted(((ucb(f, s, tf), f, s, tf) for f in fams for (s, tf) in cells), reverse=True)
                top = scored[: max(3, len(scored) // 6)]
                _, fam, sym, tf = top[int(rng.integers(len(top)))]
                try: item = self.store.get(sym, tf)
                except Exception: continue
                spec = self.inv.invent(fam, sym, tf, rng, item); note = "explore"
            if (sym, tf) not in cells: continue
            if ST.validate_spec(spec): continue
            key = (ST.spec_hash(spec), sym, tf)
            if key in seen_now or self.mem.seen(spec, sym, tf): continue
            seen_now.add(key); jobs.append(self._job(spec, sym, tf, note, fam))
        return jobs

    def _job(self, spec, sym, tf, note, fam):
        spec = {k: v for k, v in spec.items() if k in ("family", "params", "side", "stop_atr", "target_rr", "max_hold", "atr_n", "filters")}
        spec["filters"] = spec.get("filters") or []
        return dict(spec=spec, symbol=sym, timeframe=tf, note=note, hypothesis_id=self.sci.hypothesis_id(fam), _h=ST.spec_hash(spec))
