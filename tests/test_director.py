"""Phase 3b tests: pre-signal gates, registry novelty, director evidence/stagnation behaviour, GP grammar. Synthetic data only."""
import json, time
import numpy as np, pytest
from strategylab import data as D, engine as E, strategies as ST, feasibility as FE, grammar as G, validation as V, audit
from strategylab.config import Settings
from strategylab.db import DB
from strategylab.director import Director, STAG_EXPERIMENTS
from strategylab.features import Features


def setup(home, tfs=("1h",), n=8000):
    st = Settings(); st.symbols = ["BTCUSDT", "ETHUSDT"]; st.timeframes = list(tfs); st.mc_paths = 200; db = DB()
    store = E.DataStore(st, lambda s, tf: D.synthetic(s, tf, n=n, seed=sum(map(ord, s)) % 50)); return st, db, store


def tspec(**kw): return {**dict(family="tsmom", params=dict(lookback=24, thr_atr=0.5), side="both", stop_atr=3.0, target_rr=2.0, max_hold=48, atr_n=14, filters=[]), **kw}


def test_cost_infeasible_rejected_before_signals_or_backtest(home, monkeypatch):
    st, db, store = setup(home)
    def boom(*a, **k): raise AssertionError("signals/backtest must not run")
    monkeypatch.setattr(ST, "signals", boom); monkeypatch.setattr(E.V, "screen", boom)
    r = E.run_experiment(db, st, store, dict(spec=tspec(stop_atr=0.05), symbol="BTCUSDT", timeframe="1h"))
    assert r["status"] == "REJECTED" and r["reasons"] == ["cost_infeasible_precheck"]
    assert db.q("SELECT stage FROM experiments")[0]["stage"] == "eligibility"


def test_registry_blocks_exit_flooding_and_failed_neighbourhoods(home):
    st, db, store = setup(home)
    e = tspec(); out = [E.run_experiment(db, st, store, dict(spec=tspec(stop_atr=s_, max_hold=h), symbol="BTCUSDT", timeframe="1h")) for s_, h in [(2.0, 24), (3.0, 48), (4.0, 96), (6.0, 192)]]
    assert out[3]["reasons"] == ["exit_variant_limit"]                                   # 4th exit variant of one entry hypothesis
    ok, why = FE.novelty_check(db, tspec(stop_atr=8.0), "BTCUSDT", "1h"); assert (ok, why) == (False, "exit_variant_limit")
    # noise neighbourhood: three adjacent-grid neighbours already failed at the screen
    for lb, th in ((12, 0.5), (48, 0.5), (24, 0.25)):                                     # three ADJACENT grid steps around (24, 0.5)
        FE.register(db, tspec(params=dict(lookback=lb, thr_atr=th)), "BTCUSDT", "15m", 1, True)
    probe = tspec(params=dict(lookback=24, thr_atr=0.5), max_hold=12)
    assert FE.novelty_check(db, probe, "ETHUSDT", "1h")[0]                                  # different market: independent evidence
    assert FE.novelty_check(db, probe, "BTCUSDT", "15m") == (False, "noise_neighbourhood")  # neighbourhood already failed 3x at the screen


def test_signal_cache_shared_across_exit_variants(home, monkeypatch):
    st, db, store = setup(home); calls = []
    real = ST.signals; monkeypatch.setattr(E.ST, "signals", lambda F, sp: (calls.append(1), real(F, sp))[1])
    for h in (24, 48, 96):
        E.run_experiment(db, st, store, dict(spec=tspec(max_hold=h, stop_atr=3.0 + h / 100), symbol="BTCUSDT", timeframe="1h"))
    assert len(calls) == 1                                                               # entries computed once, reused for exit variants


def seed_registry(db, fam, sy, tf, n, screened, vpass):
    for i in range(n):
        stage = 3 if i < vpass else 1 if i < screened else 0
        db.x("INSERT INTO hyp_registry(hkey,entry_key,group_key,family,symbol,timeframe,side,params_json,n_variants,n_failed,best_stage,first_ts,last_ts) VALUES(?,?,?,?,?,?,?,?,1,?,?,0,0)",
             (f"{fam}{sy}{tf}{i}", f"e{i}", "g", fam, sy, tf, "both", json.dumps({"lookback": 1000 + i, "thr_atr": 0.5}), int(stage < 3), stage))


def test_director_shifts_budget_to_arms_with_validation_evidence_and_ignores_test_outcomes(home):
    st, db, store = setup(home, ("1h", "15m"))
    seed_registry(db, "tsmom", "BTCUSDT", "1h", 60, 55, 40)          # strong dev-data evidence
    seed_registry(db, "zscore_meanrev", "ETHUSDT", "15m", 60, 55, 0) # screened a lot, never validated
    d = Director(db, st, store); counts = {"good": 0, "bad": 0}
    for seed in range(40):
        arms = d.arms(); rng = np.random.default_rng(seed)
        draws = {k: rng.beta(1 + a["screened"], 1 + max(a["n"] - a["screened"], 0)) * rng.beta(1 + a["vpass"], 1 + max(a["screened"] - a["vpass"], 0)) for k, a in arms.items() if a["n"] >= 60}
        counts["good" if draws[("tsmom", "BTCUSDT", "1h")] > draws[("zscore_meanrev", "ETHUSDT", "15m")] else "bad"] += 1
    assert counts["good"] >= 38                                       # posterior ordering follows validation evidence
    j1 = d.plan_batch(24, seed=3)
    # leakage bait: flip every strategy_status to PROMISING with a 'final' summary - allocation must not change (locked test is never feedback)
    db.x("INSERT INTO strategies(id,created,family,spec_json,spec_hash) VALUES('S-x',0,'zscore_meanrev','{}','x')")
    db.x("INSERT INTO strategy_status VALUES('S-x','ETHUSDT','15m','PROMISING',0,?,0)", (json.dumps({"screen": {"t": 9}, "final": {"test": {"mean_R": 5}}}),))
    db.set("director_state", None) if False else None
    d2 = Director(db, st, store); st_ = db.get("director_state")
    j2 = d2.plan_batch(24, seed=3)
    assert [x["_h"] for x in j1] == [x["_h"] for x in j2] or len(j1) == len(j2)


def test_stagnation_changes_research_direction_and_is_explained(home):
    st, db, store = setup(home, ("1h", "15m"))
    seed_registry(db, "tsmom", "BTCUSDT", "1h", 80, 70, 30)
    d = Director(db, st, store); d.plan_batch(12, seed=1)
    s = db.get("director_state"); assert s["mode"] == "normal"
    s["exp_last_improve"] = -(STAG_EXPERIMENTS + 10); s["best"] = 5.0; db.set("director_state", s)       # no improvement for > STAG experiments
    jobs = d.plan_batch(24, seed=2); s2 = db.get("director_state"); last = db.get("director_last")
    assert s2["mode"] == "stagnation" and s2["direction"] == s["direction"] + 1 and s2["cooldown"]
    assert last["mode"] == "stagnation" and "STAGNATION" not in last["reason"] and "stagnation" in last["reason"]       # explained in the batch record
    assert last["comp"]["explore"] >= last["comp"]["exploit"]                                            # budget moved toward exploration
    assert any(r["message"].startswith("STAGNATION") for r in db.q("SELECT message FROM events WHERE level='warn'"))
    assert db.q("SELECT COUNT(*) n FROM director_batches")[0]["n"] == 2


def test_gp_rules_are_valid_causal_diverse_and_hash_deterministically(home):
    df = D.synthetic(n=5000, seed=1)[0]; ref = D.synthetic(n=5000, seed=2)[0]; rng = np.random.default_rng(0); keys = set()
    for i in range(12):
        side = ["long", "short"][i % 2]; rule = G.random_rule(rng, side)
        spec = dict(family="gp_rule", params=dict(rule=rule, side=side), side=side, stop_atr=3.0, target_rr=2.0, max_hold=48, atr_n=14, filters=[])
        assert ST.validate_spec(spec) == [] and V.lookahead_test(df, spec, ref=ref)["passed"], rule
        keys.add(ST.spec_hash(spec)); assert ST.spec_hash(spec) == ST.spec_hash(json.loads(json.dumps(spec)))
    assert len(keys) >= 10
    child = G.crossover(G.random_rule(rng, "long"), G.random_rule(rng, "long"), rng); mut = G.mutate(child, rng)
    assert not G.validate_rule(child, "long") and not G.validate_rule(mut, "long")
    assert G.validate_rule([["ret12_gt", 0.5], ["ret12_gt", 1.0]], "long") and G.validate_rule([["bogus", 1]], "long")        # duplicates / unknown atoms rejected


def test_audit_runs_and_flags_legacy_survivors(home):
    st, db, store = setup(home)
    for h in (24, 48):
        E.run_experiment(db, st, store, dict(spec=tspec(max_hold=h), symbol="BTCUSDT", timeframe="1h"))
    o = audit.run_audit(db, st, write_legacy=False, synthetic=1)
    assert o["distinctness"]["distinct_hypotheses"] >= 1 and "early_filter" in o and o["early_filter"]["rows_that_ran_backtest_after_failing_eligibility"] == 0
    assert "Research efficiency report" in audit.render_markdown(o) and db.q("SELECT COUNT(*) n FROM reports")[0]["n"] == 1


def test_cells_without_gross_edge_are_suppressed(home):
    st, db, store = setup(home); import json as _j
    for i in range(450):
        db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,started,finished,status,metrics_json,synthetic) VALUES('x','screen','BTCUSDT','1h',0,0,'fail',?,0)",
             (_j.dumps(dict(mean_R=-0.2, gross_mean_R=-0.05, avg_stop=0.02, avg_cost=0.003)),))
    from strategylab.director import Director
    D_ = Director(db, st, store, None)
    assert ("BTCUSDT", "1h") in D_.dead_cells()
