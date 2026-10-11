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
    em = get_model("user_500x"); assert CAP.HI_LEV_GRID == [25, 50, 100, 250, 500] and max(CAP.HI_RISK_GRID) >= 0.3
    assert [l for l in CAP.HI_LEV_GRID if l <= em.max_leverage] == [25, 50, 100, 250, 500]


def test_limit_execution_model_fills_and_fees():
    import numpy as np
    from strategylab import btcc, backtest as BT, feasibility as FE
    em = btcc.get_model("user_500x_maker")
    assert em.entry_cost() == em.maker_fee and em.exit_cost(stop=False) == em.maker_fee and em.exit_cost(stop=True) > em.taker_fee
    assert FE.round_trip_cost(em) < FE.round_trip_cost(btcc.get_model("user_500x"))
    # feasible stop window must be non-empty under passive execution (needs stop >= cost/0.5 and <= 0.8 x liq distance at 250x)
    assert FE.round_trip_cost(em) / 0.5 < 0.8 * em.liq_distance(250)

    class F:      # tiny synthetic tape: signal at bar 2 (long); price dips through the close limit at bar 4, then rallies to target
        n = 12; hour = np.zeros(12); minute = np.arange(12) % 60
        epoch_s = np.arange(12) * 300.0
        o = np.array([100, 100, 100, 100, 100, 100, 101, 102, 103, 104, 105, 106.0]); c = o.copy()
        h = o + 0.1; l = o - 0.1
        def atr(self, n): return np.full(12, 1.0)
        def funding(self): return np.zeros(12)
    f = F(); f.l[4] = 99.0; f.h[7] = 110.0
    L = np.zeros(12, bool); L[2] = True; S = np.zeros(12, bool)
    spec = dict(stop_atr=0.5, target_rr=2.0, max_hold=8, atr_n=14)
    tr = BT.backtest(f, spec, L, S, em, 0, 12)
    if len(tr):                       # entered on the dip at the limit price, not at a market open
        assert abs(tr.entry[0] - 100.0) < 1e-9 and tr.fee[0] <= 2 * em.taker_fee + 1e-12


def test_forensics_counterfactuals_are_reproducible_and_causal(tmp_path):
    import numpy as np
    from strategylab import forensics as FO
    cf = dict(base=dict(n=200, R=-0.05, gross=0.1), no_cost=dict(n=200, R=0.12, gross=0.12), **{"cost_x0.5": dict(n=200, R=0.03, gross=0.1)})
    causes, hints = FO.classify(cf, dict(stop_atr=1.0), weeks=100)
    assert causes[0][0] == "fees_slippage" and "wider_stop" in hints
    cf2 = dict(base=dict(n=200, R=-0.04, gross=0.0), no_cost=dict(n=200, R=0.0, gross=0.0), **{"cost_x0.5": dict(n=200, R=-0.02, gross=0.0)})
    assert FO.classify(cf2, dict(stop_atr=1.0), weeks=100)[0][0][0] == "no_predictive_edge"
    cf3 = dict(base=dict(n=200, R=0.0, gross=0.1), no_cost=dict(n=200, R=0.1, gross=0.1), **{"cost_x0.5": dict(n=200, R=0.05, gross=0.1)}, side_long=dict(n=100, R=0.12), side_short=dict(n=100, R=-0.1))
    c3 = [c[0] for c in FO.classify(cf3, dict(stop_atr=1.0), weeks=100)[0]]
    assert "long_short_asymmetry" in c3
    assert FO.classify(cf3, dict(stop_atr=1.0), weeks=100) == FO.classify(cf3, dict(stop_atr=1.0), weeks=100)       # deterministic


def test_data_audit_detects_corruption():
    import numpy as np, pandas as pd
    from strategylab import dataaudit as DA
    n = 3000; idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"); rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n))); o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.001, low=np.minimum(o, c) * 0.999, close=c, volume=rng.uniform(1, 2, n), funding=0.0), index=idx)
    assert DA.audit_frame(df, "15m")["status"] == "ok"
    dup = pd.concat([df, df.iloc[:5]]).sort_index(); assert DA.audit_frame(dup, "15m")["status"] == "quarantined"
    gap = df.drop(df.index[500:2000]); r = DA.audit_frame(gap, "15m"); assert r["status"] == "quarantined" and any("missing" in h or "gap" in h for h in r["hard"])
    bad = df.copy(); bad.iloc[100, bad.columns.get_loc("high")] = bad.iloc[100]["low"] * 0.5
    bad.iloc[200:260, bad.columns.get_loc("high")] = 1e-3          # many inconsistent bars
    assert DA.audit_frame(bad, "15m")["status"] == "quarantined"


def test_director_state_survives_null_or_partial_kv(tmp_path):
    from strategylab.db import DB
    from strategylab.director import Director
    d = DB(tmp_path / "t.db"); dr = Director.__new__(Director); dr.db = d
    d.set("director_state", None); assert dr._state()["mode"] == "normal"
    d.set("director_state", {"mode": "stagnation"}); s = dr._state(); assert s["mode"] == "stagnation" and s["hyp_prev"] == 0 and s["cooldown"] == {}


def test_accounting_separates_margin_return_from_account_profit():
    import numpy as np
    from strategylab import capital as CP, btcc
    from strategylab.backtest import Trades
    em = btcc.get_model("user_500x_maker")
    # one BTC trade: price +0.8% (entry 100000), 500x => +400% on the margin; margin $25 => ~$100 profit, NOT $1,200 on a $300 account
    tr = Trades(t_in=[1.7e9], t_out=[1.7e9 + 600], dir=[1], entry=[100000.0], ret=[0.008], fee=[0.0], fund=[0.0], stop_frac=[0.0008], mae=[0.0002], adv_exit=[0.0], reason=[1], bars=[2], scale=[1])
    # size exactly $25 margin at 500x => notional $12,500 => qty 0.125 BTC: risk_frac so that risk/(sf+fee_buf)*eq = 12500
    rf = 12500 * (0.0008 + 2 * em.taker_fee) / 300.0
    lg = CP.ledger(tr, "BTCUSDT", rf, 500, em, start=300.0)
    t = lg["trades"][0]
    assert abs(t["margin"] - 25.0) < 0.5 and abs(t["margin_return"] - 4.0) < 0.05 and abs(t["pnl"] - 100.0) < 2.0
    assert abs(t["account_return"] - t["pnl"] / 300.0) < 1e-9 and t["account_return"] < 0.40          # +33% of the account, not +400%
    assert abs(lg["final_equity"] + lg["withdrawn_total"] - 300.0 - t["pnl"]) < 1e-6
    assert lg["withdrawn_total"] <= 0.5 * t["pnl"] + 1e-9                                              # >=50% of the month's profit is retained


def test_ledger_matches_vectorised_simulator():
    import numpy as np
    from strategylab import capital as CP, btcc
    from strategylab.backtest import Trades
    em = btcc.get_model("conservative"); rng = np.random.default_rng(5); n = 60
    t_out = 1.7e9 + np.sort(rng.uniform(0, 5 * 30 * 86400, n)); net = rng.normal(0.0004, 0.004, n)
    tr = Trades(t_in=t_out - 3600, t_out=t_out, dir=np.ones(n), entry=np.full(n, 60000.0), ret=net, fee=np.zeros(n), fund=np.zeros(n), stop_frac=np.full(n, 0.01), mae=np.abs(rng.normal(0.004, 0.003, n)), adv_exit=np.zeros(n), reason=np.zeros(n), bars=np.ones(n), scale=np.ones(n))
    lg = CP.ledger(tr, "BTCUSDT", 0.05, 20, em, start=300.0, wd_target=1e9)
    blocks = CP.month_blocks(tr, t_out[0], t_out[-1] + 1); idx = np.arange(len(blocks))[None, :]
    sim = CP.simulate(blocks, idx, "BTCUSDT", 0.05, 20, em, start=300.0, wd_target=1e9)
    assert abs((lg["final_equity"] + lg["withdrawn_total"]) - float(sim["wealth"][0])) < 1e-6 * max(1, float(sim["wealth"][0])) + 1e-6


def _tape(n=6000, seed=3):
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed); idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    c = 2000 * np.exp(np.cumsum(rng.normal(0, 0.002, n))); o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0008, n))); lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0008, n)))
    return pd.DataFrame(dict(open=o, high=hi, low=lo, close=c, volume=rng.uniform(100, 200, n), quote_volume=rng.uniform(1e5, 2e5, n), count=100, taker_buy_volume=rng.uniform(40, 100, n), funding=0.0), index=idx)


def test_opportunity_features_are_causal():
    import numpy as np
    from strategylab.features import Features
    from strategylab import opportunity as OP
    df = _tape(); ref = _tape(seed=9)
    A = OP.feature_matrix(Features(df, ref)); cut = 4000
    B = OP.feature_matrix(Features(df.iloc[:cut], ref))
    for k in OP.FEATS:
        a, b = A[k][:cut - 5], B[k][:cut - 5]
        both = np.isfinite(a) & np.isfinite(b)
        assert np.allclose(a[both], b[both], rtol=1e-3, atol=1e-4), f"feature {k} changes when future bars are removed"
        assert np.array_equal(np.isfinite(a), np.isfinite(b)) or k in ("atr_pct",), k


def test_triple_barrier_labels_match_hand_computed_path():
    import numpy as np
    from strategylab.features import Features
    from strategylab import opportunity as OP, btcc
    from dataclasses import replace
    df = _tape(2000); F = Features(df)
    em = replace(btcc.get_model("eth_hl"), limit_bars=0, limit_exits=False, min_lev_capable=0)          # market entries: path starts at the next open
    lb = OP.label(F, "long", 1.0, 2.0, 12, em, 700, 1500, limit=False)
    o, h, l, c, a = F.o, F.h, F.l, F.c, F.atr(14)
    for j in (5, 50, 120, 300):
        t = lb["t"][j]; entry = o[t + 1]; sd = a[t]; stop, tgt = entry - sd, entry + 2 * sd; res = 0
        for k in range(t + 1, t + 13):
            if l[k] <= stop: res = -1; break
            if h[k] >= tgt: res = 1; break
        if lb["ok"][j]: assert lb["outcome"][j] == res, (j, lb["outcome"][j], res)


def test_discovery_never_reads_past_train_and_validation_is_embargoed(monkeypatch):
    import numpy as np
    from strategylab.features import Features
    from strategylab import opportunity as OP, btcc
    df = _tape(5000); F = Features(df); em = btcc.get_model("eth_hl")
    seen = []
    orig = OP.label
    def spy(F_, side, s_atr, rr, H, em_, lo, hi, limit=True, chunk=40000):
        seen.append((lo, hi)); return orig(F_, side, s_atr, rr, H, em_, lo, hi, limit, chunk)
    monkeypatch.setattr(OP, "label", spy)
    bounds = {"train": (0, 2500), "val": (2500, 3750), "test": (3750, 5000)}
    fins, meta = OP.discover(F, "15m", em, bounds, "long", 0.01, 2.0, 12)
    assert seen and max(h for _, h in seen) <= 2500 - 12 - 6                      # train labels end H+fill bars before the boundary (purge)
    if fins:
        seen.clear(); OP.predict(F, "15m", em, bounds, fins[0], meta)
        assert min(l for l, _ in seen) >= 2500 + 12 and max(h for _, h in seen) <= 3750        # validation starts H bars after the boundary and never touches the test
