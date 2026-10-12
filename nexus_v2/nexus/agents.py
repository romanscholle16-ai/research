"""The ten research agents. Each has its own scope (disjoint family sets), hypothesis generator, budget and history.
Agents only PROPOSE specifications; evaluation, accounting and qualification run on the shared trusted path."""
import json, random, time
from dataclasses import dataclass

from . import config, db, memory, research as R, strategies as S


@dataclass
class Proposal:
    spec: dict
    agent: str
    mode: str
    why: str
    would_change: str


SYMS = [("ETHUSDT", 0.65), ("BTCUSDT", 0.35)]
TFS = [("1h", 0.5), ("2h", 0.25), ("4h", 0.25)]


def _wchoice(rng, pairs):
    r = rng.random() * sum(w for _, w in pairs)
    for v, w in pairs:
        r -= w
        if r <= 0:
            return v
    return pairs[-1][0]


class Agent:
    name = "base"
    title = ""
    scope = ""
    families: tuple = ()
    mix = {"explore": 0.6, "refine": 0.25, "replicate": 0.15}

    def __init__(self, conn):
        self.conn = conn

    # -- shared machinery ---------------------------------------------------------------------
    def fam_weights(self):
        w = memory.weights(self.conn)
        stats = memory.family_stats(self.conn)
        out = {}
        for f in self.families:
            st = stats.get(f, {"valid": 0, "promising": 0})
            explore_bonus = 1.0 + 1.0 / (1.0 + st["valid"] / 200.0)       # untested families get more attention
            yield_bonus = 1.0 + min(2.0, st["promising"] * 0.5)           # productive families get more
            out[f] = w.get(f, 1.0) * explore_bonus * yield_bonus
        return out

    def sample(self, rng, fam, sym=None, tf=None, tries=40):
        grid = S.F[fam]["grid"]
        for _ in range(tries):
            sp = S.make_spec(fam, sym or _wchoice(rng, SYMS), tf or _wchoice(rng, TFS), {k: rng.choice(v) for k, v in grid.items()})
            try:
                S.normalize(sp)
                return sp
            except ValueError:
                continue
        return None

    def hyp(self, spec):
        return R.hypothesis_for(self.conn, self.name, spec["family"], spec["symbol"], spec["timeframe"])

    def propose(self, n, rng, seen):
        out, guard = [], 0
        fw = self.fam_weights()
        fams = list(fw)
        while len(out) < n and guard < n * 12:
            guard += 1
            mode = _wchoice(rng, list(self.mix.items()))
            p = self._propose_one(mode, rng, fams, fw, seen)
            if p is None:
                continue
            try:
                sid = S.canonical_id(p.spec)
                ver = R.explore_version(self.conn, p.spec)
            except Exception:
                continue
            if (sid, ver) in seen:
                self.conn.execute("INSERT INTO runtime_state(key,value,updated_at) VALUES('dup_rejected','1',?) ON CONFLICT(key) DO UPDATE "
                                  "SET value=CAST(value AS INTEGER)+1", (time.time(),))
                continue
            seen.add((sid, ver))
            out.append(p)
        return out

    def _propose_one(self, mode, rng, fams, fw, seen):
        fam = rng.choices(fams, [fw[f] for f in fams])[0]
        if mode == "refine":
            nm = memory.near_misses(self.conn, fam, limit=10)
            if nm:
                base = json.loads(rng.choice(nm)["spec_json"])
                nbs = S.neighbors(base)
                if nbs:
                    return Proposal(rng.choice(nbs), self.name, "refine",
                                    f"neighbour of a near-miss {fam} spec (S2 expectancy>0, below threshold): tests whether the weak edge is a stable region",
                                    "if neighbours are also positive the region is credible; if not, the near-miss was noise")
        if mode == "replicate":
            nm = memory.near_misses(self.conn, fam, limit=10)
            if nm:
                base = json.loads(rng.choice(nm)["spec_json"])
                alt_tf = rng.choice([t for t in ("1h", "2h", "4h") if t != base["timeframe"]])
                alt_sym = rng.choice(["ETHUSDT", "BTCUSDT"])
                sp = {**base, "timeframe": alt_tf, "symbol": alt_sym}
                try:
                    S.normalize(sp)
                    return Proposal(sp, self.name, "replicate", f"replicate near-miss {fam} on {alt_sym}/{alt_tf}: independent-sample check of the idea",
                                    "same-sign result elsewhere raises credibility; opposite sign suggests overfit to the original sample")
                except ValueError:
                    pass
        sp = self.sample(rng, fam)
        if sp is None:
            return None
        return Proposal(sp, self.name, "explore", f"unexplored {fam} configuration ({S.F[fam]['info']['title']})",
                        "S2 pass sends it to validation; failure adds negative evidence about this family/region")

    def recommend(self):
        return []


class MarketStructure(Agent):
    name, title = "A1_market_structure", "Market Structure"
    scope = "swing structure, breaks of structure, liquidity sweeps, regime-dependent setups"
    families = ("swing_break", "liq_sweep", "struct_pullback")


class MomentumTrend(Agent):
    name, title = "A2_momentum_trend", "Momentum and Trend"
    scope = "trend continuation, momentum persistence, breakouts, adaptive trend filters"
    families = ("donchian", "ema_cross", "tsmom", "er_breakout")


class MeanReversion(Agent):
    name, title = "A3_mean_reversion", "Mean Reversion"
    scope = "volatility-normalised extremes and regime-dependent reversion"
    families = ("zscore_revert", "rsi_extreme", "bollinger_fade")


class VolumeFlow(Agent):
    name, title = "A4_volume_flow", "Volume and Order-Flow Proxies"
    scope = "volume, taker-buy share (candle-level proxy ONLY - not order-book data), VWAP deviation"
    families = ("taker_imbalance", "volume_spike", "vwap_dev")


class BtcEth(Agent):
    name, title = "A5_btc_eth", "BTC-ETH Relationships"
    scope = "relative value, lead-lag, correlation breakdowns using timestamp-aligned closed bars"
    families = ("ratio_z", "lead_lag", "corr_break")


class VolRegime(Agent):
    name, title = "A6_vol_regime", "Volatility and Risk Regimes"
    scope = "compression/expansion, squeeze, regime-filtered signals"
    families = ("vol_expansion", "squeeze_break", "vol_filtered_trend", "highvol_revert")


class Sessions(Agent):
    name, title = "A7_sessions", "Sessions and Timing"
    scope = "hour/day-of-week/session effects, tested for stability across periods"
    families = ("hour_bias", "session_break", "dow_drift")


class PriceAction(Agent):
    name, title = "A8_price_action", "Price Action"
    scope = "inside bars, pin bars, ranges, breakout retests"
    families = ("inside_bar", "pin_bar", "range_retest")


class Hybrid(Agent):
    name, title = "A9_hybrid", "Hybrid Strategy Discovery"
    scope = "combine individually supported concepts from different agents; ablation must show added value"
    families = ("hybrid",)
    mix = {"explore": 1.0}

    def _propose_one(self, mode, rng, fams, fw, seen):
        pool = memory.near_misses(self.conn, None, limit=40, min_trades=40)
        pool = [r for r in pool if r["family"] != "hybrid" and r["t_stat"] >= 1.0 and S.F[r["family"]]["agent"] != "adversarial"]
        agents = {S.F[r["family"]]["agent"] for r in pool}
        if len(agents) < 2:
            return None                       # nothing individually supported yet - idle by design
        a = rng.choice(pool)
        cands = [r for r in pool if S.F[r["family"]]["agent"] != S.F[a["family"]]["agent"]]
        b = rng.choice(cands)
        sa, sb = json.loads(a["spec_json"]), json.loads(b["spec_json"])
        if sa["symbol"] != sb["symbol"] or sa["timeframe"] != sb["timeframe"]:
            return None
        sp = S.make_spec("hybrid", sa["symbol"], sa["timeframe"],
                         {"a": {"family": sa["family"], "params": sa["params"]}, "b": {"family": sb["family"], "params": sb["params"]},
                          "mode": rng.choice(["filter", "and_window"]), "win": rng.choice([3, 6, 12])})
        try:
            S.normalize(sp)
        except ValueError:
            return None
        return Proposal(sp, self.name, "explore", f"combine {sa['family']} (t={a['t_stat']:.2f}) with {sb['family']} (t={b['t_stat']:.2f})",
                        "must beat both components in ablation; otherwise the extra complexity is rejected as overfit")


class Adversarial(Agent):
    name, title = "A10_adversarial", "Adversarial and Alternative Discovery"
    scope = "random-entry nulls, naive baselines, direction-inverted rules, randomisation challenges, abandon recommendations"
    families = ("random_entry", "periodic_hold", "inverse")
    mix = {"explore": 1.0}

    def _propose_one(self, mode, rng, fams, fw, seen):
        k = rng.random()
        sym, tf = _wchoice(rng, SYMS), _wchoice(rng, TFS)
        if k < 0.25:
            nm = memory.near_misses(self.conn, None, limit=15, min_trades=40)
            nm = [r for r in nm if r["family"] not in ("inverse", "hybrid", "random_entry")]
            if nm:
                b = json.loads(rng.choice(nm)["spec_json"])
                sp = S.make_spec("inverse", b["symbol"], b["timeframe"], {"base": {"family": b["family"], "params": b["params"]}})
                try:
                    S.normalize(sp)
                    return Proposal(sp, self.name, "explore", f"inverse of near-miss {b['family']}: if the inverse is as good, the 'edge' is not directional skill",
                                    "inverse performing comparably => discount the original")
                except ValueError:
                    return None
        if k < 0.85:
            sp = self.sample(rng, "random_entry", sym, tf)
            return Proposal(sp, self.name, "explore", "seeded random-entry null: calibrates what exit rules/drift alone earn",
                            "null results define the bar every candidate must beat") if sp else None
        sp = self.sample(rng, "periodic_hold", sym, tf)
        return Proposal(sp, self.name, "explore", "naive periodic baseline", "baseline outperforming complex rules => complexity adds nothing") if sp else None

    def recommend(self):
        """Recommend (never enforce) reduced effort for families with strong negative evidence."""
        out = []
        null = self.conn.execute("SELECT AVG(expectancy_r) FROM experiments WHERE family='random_entry' AND trades>=30").fetchone()[0]
        for fam, st in memory.family_stats(self.conn).items():
            if S.F[fam]["agent"] == "adversarial" or st["valid"] < 150:
                continue
            if st["promising"] == 0 and (null is None or (st["avg_exp"] is not None and st["avg_exp"] <= null)):
                out.append({"family": fam, "action": "reduce_effort",
                            "reason": f"{st['valid']} valid experiments, 0 promising, mean expectancy {st['avg_exp']:.3f}R is no better than the random-entry null"})
        return out


AGENT_CLASSES = [MarketStructure, MomentumTrend, MeanReversion, VolumeFlow, BtcEth, VolRegime, Sessions, PriceAction, Hybrid, Adversarial]


def all_agents(conn):
    return [c(conn) for c in AGENT_CLASSES]
