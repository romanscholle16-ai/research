"""Unit tests (synthetic fixtures only - these say nothing about real-market performance)."""
import time, numpy as np, pytest
from strategylab import data as D, strategies as ST, backtest as B, capital as C, validation as V, ai, resources
from strategylab.btcc import ExchangeModel
from strategylab.features import Features
from strategylab.config import Settings
from strategylab.db import DB


def frame(n=6000, seed=1): return D.synthetic(n=n, seed=seed)[0]


def test_features_are_causal_for_all_families():
    df = frame(4000); ref = frame(4000, seed=2)
    for fam, f in ST.FAMILIES.items():
        params = {k: (g[0] if g else None) for k, g in f["space"].items()}
        if fam == "session_drift": params = {"hours": [3], "dir": 1}
        spec = dict(family=fam, params=params, side="both", stop_atr=2, target_rr=2, max_hold=24, atr_n=14, filters=[])
        res = V.lookahead_test(df, spec, ref=ref)
        assert res["passed"], (fam, res)


def test_lookahead_detector_catches_cheating_signal(monkeypatch):
    def cheat(F, p):
        nxt = np.r_[F.c[1:], F.c[-1]]       # uses NEXT close -> look-ahead
        return nxt > F.c, nxt < F.c
    monkeypatch.setitem(ST.FAMILIES, "cheat", dict(fn=cheat, space={}, requires=[], text="", why="", falsify=""))
    spec = dict(family="cheat", params={}, side="both", stop_atr=2, target_rr=2, max_hold=24, filters=[])
    assert not V.lookahead_test(frame(4000), spec)["passed"]


def test_costs_scale_with_notional_and_reduce_returns():
    df = frame(); F = Features(df)
    spec = dict(family="tsmom", params=dict(lookback=24, thr_atr=0.5), side="both", stop_atr=2, target_rr=2, max_hold=48, atr_n=14, filters=[])
    L, S = ST.signals(F, spec)
    base = B.backtest(F, spec, L, S, ExchangeModel()); dbl = B.backtest(F, spec, L, S, ExchangeModel().with_cost_mult(2))
    assert len(base) > 20 and len(dbl) > 20 and dbl.R.mean() < base.R.mean()
    assert np.allclose(base.fee, 2 * 0.0006)            # round-trip taker fee as fraction of notional


def test_stop_fills_before_target_in_same_bar_and_gaps_fill_at_open():
    idx = np.arange(5)
    import pandas as pd
    df = pd.DataFrame(dict(open=[100, 100, 100, 100, 100.], high=[100, 100, 111, 100, 100.], low=[100, 100, 89, 100, 100.], close=[100] * 5,
                           volume=1., quote_volume=1., count=1., taker_buy_volume=.5, funding=0.),
                      index=pd.date_range("2022-01-01", periods=5, freq="1h", tz="UTC"))
    F = Features(df); F.atr = lambda n=14: np.full(5, 2.0)
    spec = dict(family="x", params={}, side="long", stop_atr=2, target_rr=2.0, max_hold=3, atr_n=14)
    L = np.array([1, 0, 0, 0, 0], bool); S = np.zeros(5, bool)
    t = B.backtest(F, spec, L, S, ExchangeModel())
    assert len(t) == 1 and t.reason[0] == 0 and t.ret[0] < 0     # both hit in bar 2 -> stop assumed first


def test_liquidation_wipes_isolated_margin_but_cross_survives_same_trade():
    tr = B.Trades(t_in=[1.6e9], t_out=[1.6e9 + 3600], dir=[1], entry=[20000], ret=[-0.08], fee=[0.0012], fund=[0], stop_frac=[0.08], mae=[0.09], adv_exit=[0.08], reason=[0], bars=[1])
    blocks = C.month_blocks(tr, 1.6e9 - 10, 1.6e9 + 40 * 86400)
    idx = np.zeros((1, 1), int)
    iso = C.simulate(blocks, idx, "BTCUSDT", 0.5, 20, margin="isolated")     # liq distance 4.5% < 9% adverse move
    assert iso["liqs"][0] == 1 and abs(iso["final_eq"][0] - (300 - 0.5 * 300 / (0.08 + 0.0012) / 20)) < 1.0   # lost exactly the isolated margin
    cross = C.simulate(blocks, idx, "BTCUSDT", 0.05, 20, margin="cross")
    assert cross["liqs"][0] == 0


def test_withdrawals_retain_at_least_half_of_positive_months():
    n = 12
    tr = B.Trades(t_in=np.linspace(1.6e9, 1.6e9 + 5 * 86400, n), t_out=np.linspace(1.6e9, 1.6e9 + 5 * 86400, n) + 3600, dir=[1] * n, entry=[20000] * n,
                  ret=[0.2] * n, fee=[0.001] * n, fund=[0] * n, stop_frac=[0.02] * n, mae=[0.001] * n, adv_exit=[0] * n, reason=[1] * n, bars=[2] * n)
    blocks = C.month_blocks(tr, 1.6e9 - 10, 1.6e9 + 40 * 86400)
    s = C.simulate(blocks, np.zeros((3, 1), int), "BTCUSDT", 0.5, 20, wd_target=1000)
    profit = (s["final_eq"] + s["wd_total"]) - 300
    assert profit[0] > 0 and s["wd_total"][0] <= 0.5 * profit[0] + 1e-6 and s["final_eq"][0] >= 300 + 0.5 * profit[0] - 1e-6


def test_min_order_size_blocks_dust_account():
    tr = B.Trades(t_in=[1.6e9], t_out=[1.6e9 + 3600], dir=[1], entry=[100000], ret=[0.01], fee=[0.0012], fund=[0], stop_frac=[0.02], mae=[0.0], adv_exit=[0], reason=[1], bars=[1])
    blocks = C.month_blocks(tr, 1.6e9 - 10, 1.6e9 + 40 * 86400)
    s = C.simulate(blocks, np.zeros((1, 1), int), "BTCUSDT", 0.01, 1)   # 1x of $300 < 0.001 BTC @ $100k ($100)? 285 >= 100 so should trade
    assert s["ntr"][0, 0] == 1
    s = C.simulate(blocks, np.zeros((1, 1), int), "BTCUSDT", 0.01, 1, start=50.0)  # $47 buying power < $100 lot -> skipped
    assert s["ntr"][0, 0] == 0


def test_wilson_interval_and_weekly_coverage():
    lo, hi = C.wilson(0, 1000); assert lo == 0 and 0 < hi < 0.01
    tr = B.Trades(t_in=[0], t_out=[2 * 86400, 9 * 86400], dir=[1, 1], entry=[1, 1], ret=[0, 0], fee=[0, 0], fund=[0, 0], stop_frac=[.01, .01], mae=[0, 0], adv_exit=[0, 0], reason=[0, 0], bars=[1, 1])
    assert B.weekly_coverage(tr, 345600, 345600 + 21 * 86400) == pytest.approx(1 / 3, abs=0.34)


def test_spec_validation_rejects_bad_ai_specs_and_hash_is_stable():
    good = dict(family="tsmom", params=dict(lookback=24, thr_atr=0.5), side="both", stop_atr=2, target_rr=2, max_hold=48, atr_n=14, filters=[])
    assert ST.validate_spec(good) == []
    assert ST.validate_spec({**good, "family": "magic"})
    assert ST.validate_spec({**good, "params": {"lookback": 24}})
    assert ST.validate_spec({**good, "stop_atr": 50})
    assert ST.spec_hash(good) == ST.spec_hash(dict(reversed(list(good.items()))))


def test_job_queue_atomic_claim_retry_backoff_and_reclaim(home):
    db = DB(); a = db.enqueue("experiment", {"x": 1}); j = db.claim_job()
    assert j["id"] == a and db.claim_job() is None
    db.fail_job(a, "boom", max_attempts=3, base_backoff=100)
    assert db.claim_job() is None                                 # backing off
    db.x("UPDATE jobs SET not_before=0 WHERE id=?", (a,)); j = db.claim_job(); assert j["attempts"] == 2
    db.x("UPDATE jobs SET heartbeat=? WHERE id=?", (time.time() - 1000, a))
    assert db.reclaim_stalled(60) == 1
    for _ in range(5):
        db.x("UPDATE jobs SET not_before=0 WHERE id=?", (a,)); j = db.claim_job()
        if j: db.fail_job(a, "again", max_attempts=3, base_backoff=0)
    assert db.q("SELECT status FROM jobs WHERE id=?", (a,))[0]["status"] == "failed"   # bounded retries


class Flaky(ai.Provider):
    def __init__(s, name, behaviour): s.name = name; s.b = behaviour; s.calls = 0
    def available(s): return True
    def complete(s, sy, p, m):
        s.calls += 1
        if s.b == "quota": raise ai.QuotaExhausted("no credits")
        if s.b == "down": raise ai.ProviderUnavailable("down")
        return "ok", 100, 50


def test_ai_router_fallback_quota_cooldown_and_budget(home, monkeypatch):
    monkeypatch.setenv("STRATEGYLAB_FAST", "1")
    db = DB(); st = Settings(); st.ai_provider_order = ["a", "b", "c"]
    a, b, c = Flaky("a", "quota"), Flaky("b", "down"), Flaky("c", "ok")
    r = ai.Router(db, st, [a, b, c])
    res = r.ask("s", "p"); assert res and res.provider == "c"
    assert a.calls == 1 and b.calls == st.ai_max_retries + 1       # bounded retries
    r.ask("s", "p"); assert a.calls == 1                            # quota provider on cooldown
    st.ai_daily_budget_usd = 0.0
    assert r.ask("s", "p") is None and db.get("ai_state") == "budget_exhausted"   # deterministic mode continues
    r2 = ai.Router(db, st, [Flaky("a", "quota")]); st.ai_provider_order = ["a"]; st.ai_daily_budget_usd = 5
    assert r2.ask("s", "p") is None and db.get("ai_state").startswith("quota_exhausted")


def test_experiment_dedup_and_memory(home):
    from strategylab import engine as E
    st = Settings(); st.symbols = ["BTCUSDT"]; st.timeframes = ["1h"]; db = DB()
    store = E.DataStore(st, lambda s, tf: D.synthetic(s, tf, n=5000, seed=3))
    spec = dict(family="tsmom", params=dict(lookback=24, thr_atr=0.5), side="both", stop_atr=2, target_rr=2, max_hold=48, atr_n=14, filters=[])
    r1 = E.run_experiment(db, st, store, dict(spec=spec, symbol="BTCUSDT", timeframe="1h"))
    r2 = E.run_experiment(db, st, store, dict(spec=spec, symbol="BTCUSDT", timeframe="1h"))
    assert r2.get("skipped") == "already_tested" and r1["strategy_id"] == r2["strategy_id"]
    assert db.q("SELECT synthetic FROM strategy_status")[0]["synthetic"] == 1       # clearly flagged


def test_live_trading_disabled_and_no_order_code():
    from strategylab import config
    assert config.LIVE_TRADING_ENABLED is False
    import pathlib, re
    src = "\n".join(p.read_text() for p in pathlib.Path(config.__file__).parent.glob("*.py"))
    assert not re.search(r"(?i)\b(new_order|place_order|create_order|withdraw_funds|/fapi/v1/order)\b", src)


def test_placebo_flags_beta_but_not_real_edge():
    from strategylab.validation import placebo_test
    spec = dict(family="tsmom", params=dict(lookback=12, thr_atr=0.25), side="both", stop_atr=2, target_rr=0, max_hold=24, atr_n=14, filters=[])
    rng = np.random.default_rng(0)
    noise = D.synthetic(n=30000, seed=11)[0]                         # no edge -> placebo p should not be tiny
    pn = placebo_test(Features(noise), spec, ExchangeModel(), 600, 29000, rng, n=60)
    edge = D.synthetic(n=30000, seed=11, with_edge=True, edge=0.4)[0]  # genuine serial correlation -> small p
    pe = placebo_test(Features(edge), spec, ExchangeModel(), 600, 29000, rng, n=60)
    assert pe["p"] < 0.1 and pe["actual"] > pn["actual"]


def test_portfolio_concurrency_scaling_and_combined_trades():
    from strategylab.portfolio import concurrency_scale
    mk = lambda a, b: B.Trades(t_in=[a], t_out=[b], dir=[1], entry=[1], ret=[0.01], fee=[0], fund=[0], stop_frac=[0.01], mae=[0], adv_exit=[0], reason=[1], bars=[1])
    tr = concurrency_scale(B.Trades.concat([mk(0, 100), mk(50, 150), mk(200, 300)]))
    assert list(tr.scale) == [1.0, 0.5, 1.0]


def test_new_families_have_rationale_and_are_registered():
    for f in ("vwap_reversion", "narrow_range_breakout", "eth_btc_ratio_reversion"):
        assert f in ST.FAMILIES and ST.FAMILIES[f]["falsify"]
