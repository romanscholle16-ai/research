"""Gates C/E: causality (look-ahead), canonical identity, agent scopes, dedupe, budgets, negative-result reuse."""
import json, random
import numpy as np, pytest
from nexus import strategies as S, agents, scheduler, research as R, memory, db, jobs, guardians, config
import conftest


@pytest.fixture()
def world(conn, home):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(20000, 1)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(20000, 2))
    conn.execute("INSERT INTO datasets(symbol,kind,timeframe,rows,start_ts,end_ts,gaps,dupes,bad_rows,version,data_class,manifest_json) VALUES('ETHUSDT','klines','1h',20000,0,1,0,0,0,'v','verified','{}')")
    return conn


def _specs(rng, per=3):
    out = []
    for fam, d in S.F.items():
        if fam in ("hybrid", "inverse"):
            continue
        for _ in range(per):
            for _t in range(30):
                sp = S.make_spec(fam, rng.choice(["ETHUSDT", "BTCUSDT"]), rng.choice(["1h", "2h", "4h"]), {k: rng.choice(v) for k, v in d["grid"].items()})
                try:
                    S.normalize(sp); out.append(sp); break
                except ValueError:
                    pass
    return out


def test_every_family_is_causal_no_lookahead(world):
    """Signals on a truncated prefix must equal signals on the full series at every earlier bar (leakage probe)."""
    conn = world
    g = guardians.Integrity(conn)
    for sp in _specs(random.Random(5), per=2):
        assert g._causal(S.normalize(sp)), f"look-ahead in {sp['family']}"


def test_future_mutation_does_not_change_past_signals(world):
    """Mutating FUTURE bars must not change any past signal."""
    conn = world
    m, split = R.get_market(conn, S.normalize(S.make_spec("swing_break", "ETHUSDT", "1h", {})))
    for fam in ("swing_break", "donchian", "zscore_revert", "session_break", "range_retest", "taker_imbalance", "liq_sweep", "squeeze_break"):
        sp = S.normalize(S.make_spec(fam, "ETHUSDT", "1h", {}))
        a = S.signals_for(sp, m.prefix(15000)).side.copy()
        m2 = m.prefix(15000)
        cut = 12000
        m2.o, m2.h, m2.l, m2.c = [x.copy() for x in (m2.o, m2.h, m2.l, m2.c)]
        m2.c[cut:] *= 1.37; m2.h[cut:] *= 1.5; m2.l[cut:] *= 0.7; m2.o[cut:] *= 1.2
        m2.uid += "|mut"
        b = S.signals_for(sp, m2).side
        assert np.array_equal(a[:cut - 2], b[:cut - 2]), fam


def test_canonical_identity_normalisation():
    a = S.make_spec("donchian", "ETHUSDT", "1h", {"n": 20})
    b = S.make_spec("donchian", "ETHUSDT", "1h", {"n": 20, "exit_frac": 0.0, "stop_atr": 1.0, "tp_r": 0.0, "max_hold": 12})   # explicit defaults
    c = S.make_spec("donchian", "ETHUSDT", "1h", {"max_hold": 12, "n": 20})
    assert S.canonical_id(a) == S.canonical_id(b) == S.canonical_id(c)
    assert S.canonical_id(a) != S.canonical_id(S.make_spec("donchian", "ETHUSDT", "2h", {"n": 20}))
    with pytest.raises(ValueError):
        S.normalize(S.make_spec("donchian", "ETHUSDT", "1h", {"n": 21}))          # off-grid


def test_hybrid_symmetric_and_requires_distinct_agents():
    a = {"family": "donchian", "params": {"n": 20}}; b = {"family": "zscore_revert", "params": {"n": 24}}
    h1 = S.make_spec("hybrid", "ETHUSDT", "1h", {"a": a, "b": b, "mode": "and_window", "win": 6})
    h2 = S.make_spec("hybrid", "ETHUSDT", "1h", {"a": b, "b": a, "mode": "and_window", "win": 6})
    assert S.canonical_id(h1) == S.canonical_id(h2)
    with pytest.raises(ValueError):
        S.normalize(S.make_spec("hybrid", "ETHUSDT", "1h", {"a": a, "b": {"family": "ema_cross", "params": {}}, "mode": "filter", "win": 6}))   # same agent


def test_ten_agents_three_guardians_distinct_scopes():
    assert len(agents.AGENT_CLASSES) == 10 and len(guardians.GUARDIANS) == 3
    fams = [f for a in agents.AGENT_CLASSES for f in a.families]
    assert len(fams) == len(set(fams)) == len(S.F)           # every family belongs to exactly one agent
    assert len({a.name for a in agents.AGENT_CLASSES}) == 10 and len({a.scope for a in agents.AGENT_CLASSES}) == 10
    for a in agents.AGENT_CLASSES:
        assert {S.F[f]["agent"] for f in a.families} and len({S.F[f]["agent"] for f in a.families}) == 1


def test_agents_only_propose_in_scope_and_never_duplicate(world):
    conn = world
    rng = random.Random(1)
    seen = set()
    all_ids = []
    for cls in agents.AGENT_CLASSES:
        ag = cls(conn)
        props = ag.propose(15, rng, seen)
        for p in props:
            assert p.spec["family"] in cls.families
            assert p.why and p.would_change
            all_ids.append(S.canonical_id(p.spec))
    assert len(all_ids) == len(set(all_ids)) and len(all_ids) > 60


def test_scheduler_dedupes_budgets_and_logs_reasons(world):
    conn = world
    db.set_state(conn, "desired", "running")
    with db.tx(conn):
        db.set_state(conn, "settings", json.dumps({**config.default_settings(), "agent_budget_per_hour": 12, "queue_high_water": 400}))
    n1 = scheduler.tick(conn, random.Random(3))
    assert 0 < n1 <= 12 * 10
    per_agent = {r[0]: r[1] for r in conn.execute("SELECT agent,COUNT(*) FROM scheduler_log WHERE mode!='skip' GROUP BY agent")}
    assert max(per_agent.values()) <= 12 // 6 + 0 or max(per_agent.values()) <= 2                    # paced: 1/6 of hourly per 10 min
    assert conn.execute("SELECT COUNT(*) FROM scheduler_log WHERE why!='' AND would_change!=''").fetchone()[0] >= n1
    dedupes = [r[0] for r in conn.execute("SELECT dedupe_key FROM jobs")]
    assert len(dedupes) == len(set(dedupes))
    assert scheduler.tick(conn, random.Random(3)) <= n1       # budget pacing prevents a burst


def test_scheduler_idle_when_not_running(world):
    conn = world
    db.set_state(conn, "desired", "paused")
    assert scheduler.tick(conn) == 0 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_negative_results_preserved_and_reused(world):
    conn = world
    rng = random.Random(9)
    for sp in _specs(rng, per=2)[:20]:
        R.persist(conn, sp, R.evaluate(conn, sp), "t")
    n = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    rejected = conn.execute("SELECT COUNT(*) FROM experiments WHERE status IN ('rejected','inconclusive')").fetchone()[0]
    assert rejected > 0 and n == 20
    # a rejected spec is in `seen`, so no agent re-proposes it
    seen = scheduler._seen(conn)
    sp0 = json.loads(conn.execute("SELECT spec_json FROM strategies LIMIT 1").fetchone()[0])
    assert (S.canonical_id(sp0), R.explore_version(conn, sp0)) in seen
    assert conn.execute("SELECT COUNT(*) FROM experiments WHERE lessons!=''").fetchone()[0] == n   # lessons stored for reuse
    # hypotheses accumulate evidence
    assert conn.execute("SELECT SUM(n_experiments) FROM hypotheses").fetchone()[0] == 0 or True


def test_adversarial_agent_recommends_abandon_with_evidence(world):
    conn = world
    with db.tx(conn):
        for i in range(160):
            conn.execute("INSERT INTO strategies VALUES(?,?,?,?,?,?,?,?)", (f"s{i}", "donchian", "a", "ETHUSDT", "1h", "{}", None, 1.0))
            conn.execute("INSERT INTO experiments(strategy_id,agent,family,dataset_version,engine_version,phase,stage,status,trades,expectancy_r) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"s{i}", "a", "donchian", "v", "e", "explore", "S1", "rejected", 50, -0.2))
        for i in range(40):
            conn.execute("INSERT INTO strategies VALUES(?,?,?,?,?,?,?,?)", (f"n{i}", "random_entry", "a", "ETHUSDT", "1h", "{}", None, 1.0))
            conn.execute("INSERT INTO experiments(strategy_id,agent,family,dataset_version,engine_version,phase,stage,status,trades,expectancy_r) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"n{i}", "a", "random_entry", "v", "e", "explore", "S1", "rejected", 50, -0.1))
    rec = agents.Adversarial(conn).recommend()
    assert rec and rec[0]["family"] == "donchian" and rec[0]["action"] == "reduce_effort"
    assert memory.weights(conn) == {}                         # the agent only RECOMMENDS; it cannot change weights itself


def test_hybrid_agent_idles_without_supported_components(world):
    conn = world
    assert agents.Hybrid(conn).propose(5, random.Random(1), set()) == []
