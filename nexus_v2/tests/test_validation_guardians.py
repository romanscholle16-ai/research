"""Gate D (statistics/qualification) and guardian behaviour."""
import json, time
import numpy as np, pytest
from nexus import validation as V, research as R, strategies as S, qualify, guardians, db, config, data, memory, goals, metrics
import conftest

SPEC = S.make_spec("ema_cross", "ETHUSDT", "1h", {"fast": 12, "slow": 50, "stop_atr": 3.0, "max_hold": 96})


def _world(conn, home, n=40000, drift=0.0010, cls=None, seed=7):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(n, seed, drift=drift), cls=cls)
    conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(n, seed + 1, drift=drift), cls=cls)
    data._CACHE.clear(); R._EV.clear()
    sid = S.canonical_id(SPEC)
    R.persist(conn, SPEC, R.evaluate(conn, SPEC), "t")
    return sid


def test_planted_edge_passes_all_static_gates_then_waits_for_paper_forward(conn, home):
    sid = _world(conn, home)
    assert V.run_validation(conn, sid) == ["walkforward=PASS", "robustness=PASS", "holdout=PASS"]
    a = qualify.assess(conn, sid)
    assert a["gates"]["paper_forward"] == "pending" and a["status"] != "qualified"
    # no forward data yet -> in-progress evidence, never a pass
    assert V.refresh_paper_forward(conn, sid) == -1
    assert qualify.assess(conn, sid)["status"] != "qualified"


def test_full_lifecycle_to_qualified_requires_guardian3_verification(conn, home):
    sid = _world(conn, home, n=40000)
    V.run_validation(conn, sid)
    # append 150 days of forward bars from the same generating process (continuing price path)
    k_full = conftest.synth_arrays(40000 + 24 * 150, 7)
    kb_full = conftest.synth_arrays(40000 + 24 * 150, 8)
    conftest.write_dataset(home, "ETHUSDT", k_full); conftest.write_dataset(home, "BTCUSDT", kb_full)
    data._CACHE.clear(); R._EV.clear()
    r = V.refresh_paper_forward(conn, sid)
    pf = json.loads(conn.execute("SELECT metrics_json FROM validations WHERE strategy_id=? AND stage='paper_forward' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()[0])
    assert pf["forward_days"] >= 149
    assert r == 1, pf.get("failures") or pf.get("progress")
    a = qualify.assess(conn, sid)
    assert a["status"] == "awaiting_integrity_verification"          # all four gates pass, but NOT qualified yet
    g3 = guardians.Integrity(conn)
    g3.check_claims()
    assert conn.execute("SELECT status FROM candidates WHERE strategy_id=?", (sid,)).fetchone()[0] == "qualified"
    gl = goals.compute(conn, sid)
    assert gl[13]["symbol"] == "✓" and gl[14]["symbol"] == "✓" and gl[5]["symbol"] == "✓"


def test_null_data_does_not_qualify(conn, home):
    """Pure random walk (no edge): the pipeline must not produce a candidate that survives walk-forward."""
    sid = _world(conn, home, drift=0.0, seed=21)
    cand = conn.execute("SELECT 1 FROM candidates WHERE strategy_id=?", (sid,)).fetchone()
    if cand:
        V.run_validation(conn, sid)
        assert qualify.assess(conn, sid)["status"] != "qualified"


def test_false_positive_rate_on_noise_is_controlled(conn, home):
    """Run many random specs on noise: no spec may reach qualification; promotions are bounded."""
    import random
    _world(conn, home, drift=0.0, seed=33, n=30000)
    rng = random.Random(4)
    promoted = 0
    for fam in ("ema_cross", "donchian", "zscore_revert", "tsmom", "rsi_extreme", "bollinger_fade"):
        for _ in range(12):
            sp = S.make_spec(fam, "ETHUSDT", "1h", {k: rng.choice(v) for k, v in S.F[fam]["grid"].items()})
            try:
                S.normalize(sp)
            except ValueError:
                continue
            r = R.evaluate(conn, sp)
            R.persist(conn, sp, r, "t")
            if r["status"] == "promising":
                promoted += 1
                V.run_validation(conn, S.canonical_id(sp))
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE status='qualified'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM validations WHERE stage='holdout' AND passed=1").fetchone()[0] <= 1


def test_synthetic_data_class_cannot_qualify(conn, home):
    sid = _world(conn, home, cls="synthetic")
    V.run_validation(conn, sid)
    assert conn.execute("SELECT passed FROM validations WHERE strategy_id=? AND stage='walkforward'", (sid,)).fetchone()[0] == 0
    assert any("data class" in f for f in json.loads(conn.execute("SELECT metrics_json FROM validations WHERE stage='walkforward'").fetchone()[0])["failures"])


def test_missing_funding_blocks_qualification(conn, home):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(40000, 7), funding=False); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(40000, 8), funding=False)
    data._CACHE.clear(); R._EV.clear()
    sid = S.canonical_id(SPEC); R.persist(conn, SPEC, R.evaluate(conn, SPEC), "t")
    V.run_validation(conn, sid)
    assert conn.execute("SELECT passed FROM validations WHERE strategy_id=? AND stage='walkforward'", (sid,)).fetchone()[0] == 0


def test_agent_assertion_cannot_create_qualified_status(conn, home):
    sid = _world(conn, home)
    conn.execute("UPDATE candidates SET status='qualified', stage='qualified', integrity_verified=1 WHERE strategy_id=?", (sid,))   # a rogue write
    guardians.Integrity(conn).check_claims()
    row = conn.execute("SELECT status,integrity_verified FROM candidates WHERE strategy_id=?", (sid,)).fetchone()
    assert row[0] != "qualified" and row[1] == 0
    assert conn.execute("SELECT COUNT(*) FROM incidents WHERE fingerprint LIKE 'revoke:%'").fetchone()[0] == 1


def test_threshold_change_makes_evidence_stale(conn, home, monkeypatch):
    sid = _world(conn, home)
    V.run_validation(conn, sid)
    assert qualify.assess(conn, sid)["gates"]["walkforward"] == "passed"
    monkeypatch.setitem(config.THRESHOLDS, "wf_min_trades", 1)          # someone loosens a threshold
    a = qualify.assess(conn, sid)
    assert a["gates"]["walkforward"] == "stale" and a["status"] != "qualified"


def test_guardian1_detects_trusted_config_tamper_and_pauses(conn, home, monkeypatch):
    db.set_state(conn, "desired", "running")
    g = guardians.Reliability(conn)
    g.cycle()
    assert db.get_state(conn, "desired") == "running"
    monkeypatch.setitem(config.THRESHOLDS, "s2_min_t", 0.1)
    g.last.clear(); g.cycle()
    assert db.get_state(conn, "desired") == "paused"
    assert conn.execute("SELECT severity FROM incidents WHERE fingerprint='config_changed'").fetchone()[0] == "critical"


def test_guardian3_detects_trusted_code_change(conn, home, monkeypatch):
    g = guardians.Integrity(conn)
    g.check_hashes()                                         # records baseline
    monkeypatch.setattr(guardians, "trusted_code_hash", lambda: "tampered")
    g.last.clear(); g.check_hashes()
    assert conn.execute("SELECT COUNT(*) FROM incidents WHERE fingerprint='code_changed'").fetchone()[0] == 1


def test_controlled_change_requires_passing_tests(conn):
    with pytest.raises(PermissionError):
        guardians.approve_code_baseline(conn, "x", {"failed": 2})
    guardians.approve_code_baseline(conn, "reviewed", {"failed": 0})


def test_guardian3_reproducibility_and_counter_checks_pass_on_clean_data(conn, home):
    _world(conn, home)
    g = guardians.Integrity(conn)
    g.check_reproducibility(); g.check_counters(); g.check_causality(); g.check_holdout()
    bad = conn.execute("SELECT check_name,detail FROM guardian_log WHERE ok=0").fetchall()
    assert not bad, [tuple(b) for b in bad]


def test_guardian3_catches_nonreproducible_result(conn, home):
    sid = _world(conn, home)
    conn.execute("UPDATE experiments SET net=net+5 WHERE strategy_id=?", (sid,))     # tampered stored result
    g = guardians.Integrity(conn)
    for _ in range(5):
        g.check_reproducibility()
        if conn.execute("SELECT 1 FROM guardian_log WHERE check_name='reproducibility' AND ok=0").fetchone():
            break
    assert conn.execute("SELECT COUNT(*) FROM incidents WHERE fingerprint LIKE 'repro:%'").fetchone()[0] >= 1


def test_guardian2_redirects_budget_but_never_weakens_thresholds(conn, home):
    db.set_state(conn, "desired", "running"); db.set_state(conn, "running_since", time.time() - 7200)
    now = time.time()
    with db.tx(conn):
        for i in range(60):
            conn.execute("INSERT INTO strategies VALUES(?,?,?,?,?,?,?,?)", (f"h{i}", "hybrid", "a", "ETHUSDT", "1h", "{}", None, 1.0))
            conn.execute("INSERT INTO experiments(strategy_id,agent,family,dataset_version,engine_version,phase,stage,status,trades,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"h{i}", "a", "hybrid", "v", "e", "explore", "S1", "inconclusive", 2, now - 100))
        for i in range(10):
            conn.execute("INSERT INTO strategies VALUES(?,?,?,?,?,?,?,?)", (f"w{i}", "donchian", "a", "ETHUSDT", "1h", "{}", None, 1.0))
            conn.execute("INSERT INTO candidates(strategy_id,stage,status,promoted_at,updated_at) VALUES(?,?,?,?,?)", (f"w{i}", "development", "promising", 1, 1))
            conn.execute("INSERT INTO validations(strategy_id,stage,passed,n_trades,metrics_json,thresholds_hash,engine_version,dataset_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (f"w{i}", "walkforward", 0, 10, "{}", "h", "e", "v", 1.0))
    h0 = config.trusted_hash()
    g = guardians.Stagnation(conn)
    g._reduce("hybrid", 0.5, "test")
    for r in conn.execute("SELECT s.family, COUNT(*) n, SUM(v.passed) p FROM validations v JOIN strategies s ON s.id=v.strategy_id WHERE v.stage='walkforward' GROUP BY s.family HAVING n>=10 AND p=0"):
        g._reduce(r["family"], 0.5, "overfit family")
    w = memory.weights(conn)
    assert w["hybrid"] < 1 and w["donchian"] < 1 and min(w.values()) >= 0.05
    assert config.trusted_hash() == h0                        # thresholds untouched by redirection
    assert conn.execute("SELECT COUNT(*) FROM stagnation_events").fetchone()[0] >= 2


def test_bootstrap_and_montecarlo_sane():
    rng = np.random.default_rng(0)
    r = rng.normal(0.3, 1.0, 400)
    lo, hi = metrics.block_bootstrap_mean(r)
    assert lo < r.mean() < hi and lo > 0
    mc = metrics.monte_carlo(r, 0.01, 300.0, 500, 100, 0.5)
    assert mc["p_ruin"] < 0.02 and mc["dd_p95"] < 0.35
    bad = metrics.monte_carlo(rng.normal(-0.3, 1.0, 400), 0.05, 300.0, 500, 200, 0.5)
    assert bad["p_ruin"] > 0.3


# ---------------------------------------------------------------- calibration regressions (post-launch finding) ---------
def test_multiple_testing_bar_is_independent_of_arrival_order(conn, home):
    """Early candidates must not enjoy a lenient bar: a fresh DB (M_seen=1) still demands sqrt(2 ln 50) ~ 2.80."""
    import math
    assert V._wf_candidates_tested(conn) >= config.THRESHOLDS["wf_expected_candidates"] == 50
    sid = _world(conn, home)
    from nexus import validation
    passed, mt, *_ = validation.walkforward(conn, sid, S.normalize(SPEC), *R.get_market(conn, S.normalize(SPEC)))
    assert mt["need_t"] == pytest.approx(math.sqrt(2 * math.log(50)), rel=1e-6) and mt["M"] == 50


def test_exit_rule_drift_is_not_mistaken_for_entry_skill(conn, home):
    """Weak trending data: random entries with the same stops/holds are also profitable, so a plain EMA cross must FAIL the
    out-of-sample randomisation test even though it passes walk-forward."""
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(40000, 7, drift=0.0006, regime_flip=1 / 300))
    conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(40000, 8, drift=0.0006, regime_flip=1 / 300))
    data._CACHE.clear(); R._EV.clear()
    sid = S.canonical_id(SPEC); R.persist(conn, SPEC, R.evaluate(conn, SPEC), "t")
    notes = V.run_validation(conn, sid)
    assert notes[0] == "walkforward=PASS" and notes[1] == "robustness=FAIL"
    f = json.loads(conn.execute("SELECT metrics_json FROM validations WHERE strategy_id=? AND stage='robustness'", (sid,)).fetchone()[0])["failures"]
    assert any("randomisation" in x for x in f)
    assert conn.execute("SELECT COUNT(*) FROM holdout_access WHERE passed>=0").fetchone()[0] == 0     # the holdout was NOT spent on it


def test_holdout_requires_statistical_distinguishability(conn, home, monkeypatch):
    sid = _world(conn, home)
    monkeypatch.setitem(config.THRESHOLDS, "ho_min_t", 1e6)
    V.run_validation(conn, sid)
    mt = json.loads(conn.execute("SELECT metrics_json FROM validations WHERE strategy_id=? AND stage='holdout'", (sid,)).fetchone()[0])
    assert any("not distinguishable from zero" in f for f in mt["failures"])


def test_goal6_needs_holdout_not_just_walkforward(conn, home):
    sid = _world(conn, home)
    ns = S.normalize(SPEC); m, split = R.get_market(conn, ns)
    for stage, fn in (("walkforward", V.walkforward), ("robustness", V.robustness)):                  # stages 3+4 only, no holdout
        passed, mt, runs, extra = fn(conn, sid, ns, m, split)
        with db.tx(conn):
            V._record(conn, sid, stage, passed, 1, mt, m.version, runs)
    conn.execute("UPDATE candidates SET stage='robustness' WHERE strategy_id=?", (sid,))
    g6 = goals.compute(conn, sid)[5]
    assert g6["symbol"] == "◐" and "holdout" in g6["obstacle"]


def test_controlled_config_change_marks_evidence_stale_and_holdout_stays_spent(conn, home, monkeypatch):
    sid = _world(conn, home)
    V.run_validation(conn, sid)
    assert qualify.assess(conn, sid)["gates"]["holdout"] == "passed"
    monkeypatch.setitem(config.THRESHOLDS, "wf_min_trades", 59)                    # a reviewed protocol change
    with pytest.raises(PermissionError):
        guardians.approve_config_baseline(conn, "x", {"failed": 1})                  # no passing tests -> refused
    diff = guardians.approve_config_baseline(conn, "calibration", {"failed": 0})
    assert diff == {"thresholds.wf_min_trades": {"old": 60, "new": 59}}
    a = qualify.assess(conn, sid)
    assert a["gates"]["walkforward"] == "stale" and a["status"] != "qualified"
    notes = V.run_validation(conn, sid)                                              # stale stages are re-run under the new protocol...
    assert notes[0].startswith("walkforward=") and any("holdout denied: candidate already evaluated" in n for n in notes)   # ...but the holdout look is spent
    assert qualify.assess(conn, sid)["status"] != "qualified"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='change_control'").fetchone()[0] == 1


def test_periodic_sweep_requeues_stale_candidates_once_per_config_version(conn, home):
    from nexus import scheduler
    sid = _world(conn, home)
    V.run_validation(conn, sid)
    scheduler.periodic(conn)
    scheduler.periodic(conn)                                   # idempotent within a config version
    keys = [r[0] for r in conn.execute("SELECT dedupe_key FROM jobs WHERE type='validate'")]
    assert keys.count(f"validate:{sid}:{config.trusted_hash()[:8]}") == 1
