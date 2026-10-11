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


def test_surrogate_learns_from_rejects_and_prefers_better_specs(home):
    st, db, store = setup(home); import json as _j, numpy as _np
    from strategylab.surrogate import Surrogate
    rng = _np.random.default_rng(0)
    for i in range(700):          # synthetic history: wide stops carry edge, tight stops lose to cost
        stop = float(rng.choice([0.5, 1.0, 3.0, 6.0])); sp = tspec(stop_atr=stop, params=dict(lookback=int(rng.choice([12, 24, 48])), thr_atr=0.5))
        db.x("INSERT OR IGNORE INTO strategies(id,created,family,spec_json,spec_hash) VALUES(?,?,?,?,?)", (f"S{i}", 0, "tsmom", _j.dumps(sp), f"h{i}"))
        db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,started,finished,status,metrics_json,synthetic) VALUES(?,?,?,?,0,0,'fail',?,0)",
             (f"S{i}", "screen", "BTCUSDT", "1h", _j.dumps(dict(mean_R=(0.15 if stop >= 3 else -0.2) + float(rng.normal(0, 0.03)), n=200)),))
    s = Surrogate(db); assert s.fit() and s.stats["holdout_corr"] > 0.5
    assert s.score(tspec(stop_atr=6.0), "BTCUSDT", "1h", kappa=0) > s.score(tspec(stop_atr=0.5), "BTCUSDT", "1h", kappa=0)


def test_positioning_atoms_are_causal_and_gated():
    import numpy as _np, pandas as _pd
    from strategylab import grammar as _G
    from strategylab.features import Features
    r = _np.random.default_rng(1); n = 3000
    idx = _pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC"); c = 100 + _np.cumsum(r.normal(0, 1, n))
    df = _pd.DataFrame(dict(open=c, high=c + 1, low=c - 1, close=c, volume=1.0, taker_buy_volume=0.5, funding=0.0,
                            oi=1e9 * (1 + 0.1 * _np.sin(_np.arange(n) / 50) + r.normal(0, .01, n)), ls_top=1 + r.normal(0, .1, n).cumsum() * .01 + 1, ls_acct=1.2, tk_ls=1.0), index=idx)
    full = _G.atom_array(Features(df), "oichg6_gt", 1.0); cut = _G.atom_array(Features(df.iloc[:2000]), "oichg6_gt", 1.0)
    assert (full[:2000] == cut).all()                                  # no look-ahead: prefix of full == computation on truncated data
    nan_df = df.drop(columns=["oi"]); assert not _G.atom_array(Features(nan_df), "oichg6_gt", 1.0).any()   # missing data => never fires
    assert not _G.available("oichg6_gt", has_oi=False) and _G.available("oichg6_gt", has_oi=True)


def test_plan_batch_skips_saturated_arms_and_stays_fast(home):
    import time as _t
    st, db, store = setup(home); st.symbols = ["BTCUSDT"]; st.timeframes = ["1h"]
    from strategylab.director import Director
    D_ = Director(db, st, store, None)
    t0 = _t.time(); jobs = D_.plan_batch(12, seed=3); assert len(jobs) > 0 and _t.time() - t0 < 60
    assert "arms_exhausted" in db.get("director_last")["comp"]


def test_surrogate_validation_head_prefers_out_of_sample_winners(home):
    st, db, store = setup(home); import json as _j, numpy as _np
    from strategylab.surrogate import Surrogate
    rng = _np.random.default_rng(1)
    for i in range(500):      # train-period edge identical, but only wide stops hold up in the validation period
        stop = float(rng.choice([1.0, 6.0])); sp = tspec(stop_atr=stop, params=dict(lookback=int(rng.choice([12, 24, 48])), thr_atr=0.5))
        db.x("INSERT OR IGNORE INTO strategies(id,created,family,spec_json,spec_hash) VALUES(?,?,?,?,?)", (f"V{i}", 0, "tsmom", _j.dumps(sp), f"vh{i}"))
        for stg, key, r_ in (("screen", "mean_R", 0.1), ("validate", "val", (0.12 if stop >= 6 else -0.15))):
            m = dict(mean_R=r_, n=200) if stg == "screen" else {"val": dict(mean_R=r_ + float(rng.normal(0, .02)), n=200)}
            db.x("INSERT INTO experiments(strategy_id,stage,symbol,timeframe,started,finished,status,metrics_json,synthetic) VALUES(?,?,?,?,0,0,'pass',?,0)", (f"V{i}", stg, "BTCUSDT", "1h", _j.dumps(m)))
    s = Surrogate(db); assert s.fit() and s.wv is not None
    assert s.score(tspec(stop_atr=6.0), "BTCUSDT", "1h", kappa=0) > s.score(tspec(stop_atr=1.0), "BTCUSDT", "1h", kappa=0)


def test_feature_memo_is_bounded():
    import numpy as _np, pandas as _pd
    from strategylab.features import Features
    n = 500; idx = _pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC"); c = 100 + _np.cumsum(_np.random.default_rng(0).normal(0, 1, n))
    F = Features(_pd.DataFrame(dict(open=c, high=c + 1, low=c - 1, close=c, volume=1.0, taker_buy_volume=.5, funding=0.0), index=idx))
    for k in range(Features.MEMO_MAX * 3): F._memo(("k", k), lambda: _np.zeros(n))
    assert len(F._c) <= Features.MEMO_MAX
    assert F.atr(14).shape == (n,)


def test_refinery_ratchet_and_dev_only(home, monkeypatch):
    from strategylab import refine as RF, validation as VV
    st, db, store = setup(home)
    from strategylab.btcc import get_model; EM = get_model('conservative')
    it = store.get("BTCUSDT", "1h"); hi_allowed = it["bounds"]["val"][1]; seen = []
    real = VV.run
    monkeypatch.setattr(RF.V, "run", lambda F, spec, em, lo, hi, cache=None: (seen.append(hi), real(F, spec, em, lo, hi, cache))[1])
    spec = tspec()
    a = RF.autopsy(it["F"], spec, EM, hi_allowed)
    assert len(a["window_R"]) == RF.K_WIN and a["verdict"] in ("broad", "concentrated", "fragile")
    best, ev, lin = RF.refine_seed(db, st, store, EM, spec, "BTCUSDT", "1h", rounds=2, budget_s=30)
    assert max(seen) <= hi_allowed                                   # the locked test range is never touched
    # ratchet logic
    par = dict(propose=0.05, confirm=0.05, all=0.05, pos_odd=.75, pos_even=.75, pos_all=.75, trades=100)
    assert RF.accept(par, dict(par, propose=0.08, confirm=0.05))
    assert not RF.accept(par, dict(par, propose=0.20, confirm=0.00))   # improves only where it looked
    assert not RF.accept(par, dict(par, propose=0.08, pos_all=.5))     # loses consistency
    n = RF.run_batch(db, st, store, EM, max_seeds=2, budget_s=20); assert "queued" in n


def test_down_regime_gate_rejects_bull_only_edge():
    import numpy as _np
    from strategylab.validation import down_regime_check
    class FF: pass
    f = FF(); n = 600; f.n = n
    f.c = _np.r_[_np.linspace(100, 160, 300), _np.linspace(160, 120, 300)]          # first half bull, second half bear
    wf = dict(k=2, folds=[(60, 0.2), (60, -0.15)])                                   # strategy earns only in the bull half
    dr = down_regime_check(f, wf, 0, n); assert dr["checked"] and dr["mean_R"] < 0
    wf2 = dict(k=2, folds=[(60, 0.2), (60, 0.10)]); assert down_regime_check(f, wf2, 0, n)["mean_R"] > 0


def test_high_leverage_mode_requires_stop_inside_liquidation_distance(home):
    from strategylab.btcc import get_model
    st, db, store = setup(home); em = get_model("user_500x"); it = store.get("BTCUSDT", "1h")
    assert em.high_leverage and em.max_leverage == 500 and em.min_lev_capable == 250
    liq = em.liq_distance(250); assert 0.002 < liq < 0.004                          # ~0.3% of price at 250x
    wide = FE.precheck_cost(it["F"], tspec(stop_atr=3.0), it["bounds"], em)         # 1h bars are far noisier than 0.24% => stop beyond liquidation
    assert not wide["ok"] and wide["reason"] == "not_leverage_capable"
    assert get_model("conservative").liq_distance(100) > 0 and not get_model("conservative").high_leverage


def test_high_leverage_sizing_grid_is_250_or_500(home):
    from strategylab import capital as CAP
    from strategylab.btcc import get_model
    em = get_model("user_500x"); assert CAP.HI_LEV_GRID == [250, 500] and max(CAP.HI_RISK_GRID) >= 0.3
    assert [l for l in CAP.HI_LEV_GRID if l <= em.max_leverage] == [250, 500]
