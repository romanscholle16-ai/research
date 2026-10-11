"""Acceptance tests for zero-paid-API mode and the target-driven funnel (synthetic fixtures; say nothing about real-market alpha)."""
import os, subprocess, sys, time
import numpy as np, pytest
from strategylab import ai, data as D, engine as E, strategies as ST, feasibility as FE, validation as V, agents as A, dashboard
from strategylab.btcc import ExchangeModel
from strategylab.config import Settings
from strategylab.db import DB
from strategylab.features import Features


class PaidSpy(ai.Provider):
    name = "anthropic"; paid = True; calls = 0
    def available(self): return True
    def complete(self, sy, p, m): PaidSpy.calls += 1; return "[]", 1, 1


def settings_for(*tfs):
    st = Settings(); st.symbols = ["BTCUSDT"]; st.timeframes = list(tfs or ["1h"]); st.mc_paths = 200; return st


# 1 + 2 ---------------------------------------------------------------------------------------------------------------------------------
def test_paid_providers_blocked_by_default_even_with_keys_present(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKEKEYFAKEKEY123"); monkeypatch.setenv("OPENAI_API_KEY", "sk-FAKEKEYFAKEKEY123")
    monkeypatch.delenv("STRATEGYLAB_ALLOW_PAID", raising=False)
    db = DB(); st = Settings(); st.ai_provider_order = ["anthropic", "openai_compat", "ollama"]; PaidSpy.calls = 0
    r = ai.Router(db, st, [PaidSpy(), ai.OpenAICompat()])
    assert st.allow_paid_providers is False and Settings().ai_provider_order == ["ollama"]      # default config is free-only
    assert r.ask("s", "p") is None and PaidSpy.calls == 0                                       # nothing was called
    assert "paid APIs disabled" in r.mode() and r.status()["paid_enabled"] is False
    assert all(p["blocked"] for p in r.status()["providers"])
    st.allow_paid_providers = True                                                              # setting alone is not enough ...
    assert r.ask("s", "p") is None and PaidSpy.calls == 0
    monkeypatch.setenv("STRATEGYLAB_ALLOW_PAID", "1")                                           # ... both gates are required
    assert r.ask("s", "p") is not None and PaidSpy.calls == 1


def test_system_runs_with_no_api_key_and_no_local_model(home, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STRATEGYLAB_ALLOW_PAID"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")                                      # nothing listens there
    st = settings_for(); db = DB(); r = ai.Router(db, st)
    store = E.DataStore(st, lambda s, tf: D.synthetic(s, tf, n=6000, seed=2)); sup = A.Supervisor(db, st, r, store)
    jobs = sup.plan(6, use_ai=True)
    assert len(jobs) >= 3 and db.get("ai_state") in ("paid_blocked_no_local_model", "none_available")
    for j in jobs: E.run_experiment(db, st, store, j)
    assert db.counts()["experiments"] >= 3 and not db.q("SELECT 1 FROM ai_usage")               # no AI usage recorded at all


# 3 + 4 + 5 -----------------------------------------------------------------------------------------------------------------------------
def register_rare(monkeypatch, every):
    def rare(F, p):
        L = np.zeros(F.n, bool); L[1000::every] = True; return L, np.zeros(F.n, bool)
    monkeypatch.setitem(ST.FAMILIES, "rare", dict(fn=rare, space={"x": [1]}, requires=[], text="", why="", falsify=""))


def spec(**kw):
    return {**dict(family="rare", params={"x": 1}, side="both", stop_atr=2.0, target_rr=2.0, max_hold=24, atr_n=14, filters=[]), **kw}


def test_low_frequency_candidates_are_rejected_before_any_backtest(home, monkeypatch):
    register_rare(monkeypatch, every=300)                                                       # ~1 signal per 12.5 days on 1h bars
    st = settings_for(); db = DB(); store = E.DataStore(st, lambda s, tf: D.synthetic(s, tf, n=20000, seed=3))
    def boom(*a, **k): raise AssertionError("backtest must not run for ineligible candidates")
    monkeypatch.setattr(V, "screen", boom); monkeypatch.setattr(E.V, "screen", boom)
    res = E.run_experiment(db, st, store, dict(spec=spec(), symbol="BTCUSDT", timeframe="1h"))
    assert res["status"] == "REJECTED" and "insufficient_frequency" in res["reasons"]
    row = db.q("SELECT stage, status FROM experiments WHERE strategy_id=?", (res["strategy_id"],))
    assert [r["stage"] for r in row] == ["eligibility"] and row[0]["status"] == "fail"           # recorded, with a reason
    assert db.q("SELECT summary_json FROM strategy_status")[0]["summary_json"].count("frequency") >= 1


def test_frequency_counts_completed_trades_not_candles(home, monkeypatch):
    def everybar(F, p): return np.ones(F.n, bool), np.zeros(F.n, bool)                          # signal on EVERY candle
    monkeypatch.setitem(ST.FAMILIES, "everybar", dict(fn=everybar, space={"x": [1]}, requires=[], text="", why="", falsify=""))
    df = D.synthetic(n=20000, seed=4)[0]; F = Features(df)
    b = {"train": (600, 10000), "val": (10000, 15000), "test": (15000, 20000)}
    a = FE.stage_a(F, spec(family="everybar", max_hold=48), b, ExchangeModel(), Settings())
    assert a["signal_bars_per_week"] > 100                                                      # candles: ~168/week
    assert a["trades_week_expected"] < a["signal_bars_per_week"] / 10                           # completed trades: hold ~16 bars each
    assert a["trades_week_upper"] <= a["signal_bars_per_week"] / 2 + 0.5
    assert abs(a["trades_week_expected"] - 168 / 16) < 1.5


def test_costs_enter_the_economic_screen(home, monkeypatch):
    register_rare(monkeypatch, every=20)
    df = D.synthetic(n=20000, seed=5)[0]; F = Features(df); b = {"train": (600, 10000), "val": (10000, 15000), "test": (15000, 20000)}
    base = FE.stage_a(F, spec(), b, ExchangeModel(), Settings()); dear = FE.stage_a(F, spec(), b, ExchangeModel().with_cost_mult(3), Settings())
    assert dear["round_trip_cost"] == pytest.approx(3 * base["round_trip_cost"], rel=1e-2) and dear["cost_R"] > base["cost_R"] * 2.9
    assert dear["required_gross_edge_R"] > base["required_gross_edge_R"]
    assert 0.55 < base["required_monthly_return"] < 0.7                                         # ~61%/month for $1k/month from $300
    tight = FE.stage_a(F, spec(stop_atr=0.2), b, ExchangeModel(), Settings())                   # tiny stop => costs swamp the stop distance
    assert "cost_infeasible" in tight["reasons"] and tight["failure_class"] == "costs"


# 6 --------------------------------------------------------------------------------------------------------------------------------------
def test_exit_only_variants_of_a_failed_hypothesis_are_not_retested_forever(home):
    st = settings_for(); db = DB(); store = E.DataStore(st, lambda s, tf: D.synthetic(s, tf, n=8000, seed=6))
    base = dict(family="zscore_meanrev", params={"n": 20, "k": 1.5}, side="both", atr_n=14, filters=[])
    out = []
    for rr, hold, stop in [(1.0, 12, 1.5), (1.5, 24, 2.0), (2.0, 48, 3.0), (3.0, 96, 1.0), (0, 24, 2.0), (2.0, 12, 3.0)]:
        out.append(E.run_experiment(db, st, store, dict(spec=dict(base, target_rr=rr, max_hold=hold, stop_atr=stop), symbol="BTCUSDT", timeframe="1h")))
    assert all(o["status"] == "REJECTED" for o in out)                                           # noise data: nothing should pass
    assert any("near_duplicate_failed_hypothesis" in o["reasons"] for o in out[3:])
    assert sum("near_duplicate_failed_hypothesis" in o["reasons"] for o in out[:3]) == 0


# 8 --------------------------------------------------------------------------------------------------------------------------------------
def test_second_service_does_not_start_while_one_is_alive(home):
    from strategylab.service import Service
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (home / "service.pid").write_text(str(holder.pid))
        with pytest.raises(SystemExit): Service().acquire()                                      # pid lock => no duplicate service/worker
    finally:
        holder.kill()


def test_history_is_preserved_when_schema_is_reopened(home):
    db = DB(); db.x("INSERT INTO strategies(id,created,family,spec_json,spec_hash) VALUES('S-keep',0,'tsmom','{}','h')")
    DB()                                                                                         # re-running the schema must not drop data
    assert DB().q("SELECT id FROM strategies")[0]["id"] == "S-keep"


# 9 + 10 ---------------------------------------------------------------------------------------------------------------------------------
def test_dashboard_reports_mode_service_and_last_experiment(home):
    db = DB(); db.set("control", "running"); db.set("last_experiment", {"status": "REJECTED", "ts": time.time()}); db.set("service_heartbeat", time.time())
    s = dashboard.status(db)
    assert s["paid_apis_enabled"] is False and "paid APIs disabled" in s["research_mode"] and s["control"] == "running"
    assert s["last_experiment"]["status"] == "REJECTED" and s["live_trading_enabled"] is False
    html = open(os.path.join(os.path.dirname(dashboard.__file__), "dashboard.html")).read()
    assert "Paid API calls" in html and "Last completed" in html


def test_qualification_gates_unchanged():
    q = V.qualification_status
    assert q({"screen": [], "validate": [], "final": []}) == "BACKTEST_QUALIFIED"
    assert q({"screen": [], "validate": [], "final": ["fails_multiple_testing"]}) == "PROMISING"
    assert q({"screen": [], "validate": [], "final": ["inadequate_trade_frequency"]}) == "PROMISING"
    assert q({"screen": [], "validate": [], "final": ["fails_locked_test"]}) == "REJECTED"
    assert q({"screen": [], "validate": [], "final": ["depends_on_few_trades"]}) == "REJECTED"
    assert q({"screen": ["no_edge"], "validate": [], "final": None}) == "REJECTED"
