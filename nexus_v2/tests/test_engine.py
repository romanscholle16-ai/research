"""Hand-calculated reference cases for the trusted backtester (Gate C)."""
import numpy as np, pytest
from nexus import engine as E
from conftest import make_market, sig

COSTS = E.Costs(taker_fee=0.001, slippage_bps=0.0, half_spread_bps=0.0, funding_assumed_rate_8h=0.0001, mmr=0.004,
                min_notional=1.0, qty_step=0.001)
ACC = lambda **k: E.Account(start_equity=1000.0, risk_pct=0.01, leverage=10.0, **k)


def flat(n=12, p=100.0):
    return [p] * n


def test_stop_hit_long_hand_calc():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.0] * n, flat(n)
    l[2] = 97.0; o[2] = 99.0; c[2] = 98.5
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(), COSTS)
    t = r.trades
    # notional = 10/(0.02+0.002)=454.545 ; qty=floor(4.54545/0.001)*0.001=4.545 ; notional=454.5
    assert t["qty"][0] == pytest.approx(4.545)
    assert t["exit_px"][0] == pytest.approx(98.0)                      # stop, no gap, no slippage
    gross = (98 - 100) * 4.545
    fees = 0.001 * 454.5 + 0.001 * 4.545 * 98.0
    assert t["gross"][0] == pytest.approx(gross)
    assert t["fees"][0] == pytest.approx(fees)
    assert t["net"][0] == pytest.approx(gross - fees)
    assert t["r"][0] == pytest.approx((gross - fees) / 10.0)
    assert t["reason"][0] == "stop"


def test_target_hit_and_no_price_improvement():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.5] * n, flat(n)
    h[3] = 110.0                                            # way through target 104 (tp_r=2, stop 2)
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0, tp_r=2.0), ACC(), COSTS)
    assert r.trades["exit_px"][0] == pytest.approx(104.0)  # never better than the target
    assert r.trades["reason"][0] == "target"
    assert r.trades["net"][0] > 0


def test_same_bar_stop_and_target_stop_wins():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.5] * n, flat(n)
    h[2] = 110.0; l[2] = 90.0
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0, tp_r=2.0), ACC(), COSTS)
    assert r.trades["reason"][0] == "stop" and r.trades["net"][0] < 0


def test_gap_through_stop_fills_at_worse_open():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.5] * n, flat(n)
    o[3] = 95.0; h[3] = 96.0; l[3] = 94.0; c[3] = 95.0
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(), COSTS)
    assert r.trades["exit_px"][0] == pytest.approx(95.0)


def test_short_pnl_and_slippage_direction():
    n = 12
    o, h, l, c = flat(n), [100.5] * n, [99.5] * n, flat(n)
    l[2] = 90.0
    m = make_market(o, h, l, c)
    c2 = E.Costs(taker_fee=0.0, slippage_bps=10.0, half_spread_bps=0.0, mmr=0.004, min_notional=1.0, qty_step=0.001)
    r = E.run_backtest(m, sig(n, 0, -1, 2.0, tp_r=3.0), ACC(), c2)
    t = r.trades
    assert t["entry_px"][0] == pytest.approx(100 * (1 - 0.001))   # short entry slips DOWN (adverse)
    assert t["exit_px"][0] == pytest.approx(93.9)                 # target = fill(99.9) - 3*2 ; limit: no slippage
    assert t["gross"][0] == pytest.approx((100 - 93.9) * t["qty"][0])
    assert t["slip"][0] == pytest.approx(0.1 * t["qty"][0])       # 10 bps entry slip on 100
    assert t["slip"][0] > 0                                       # slippage reported as a cost
    assert t["net"][0] == pytest.approx(t["gross"][0] - t["slip"][0])


def test_funding_long_pays_short_receives():
    n = 20
    o, h, l, c = flat(n), [100.2] * n, [99.8] * n, flat(n)
    m0 = make_market(o, h, l, c)
    ft = [int(m0.t[4]) + 1]                                   # one funding event while holding
    for side, sign in ((1, -1), (-1, +1)):
        m = make_market(o, h, l, c, fund_t=ft, fund_r=[0.0005])
        r = E.run_backtest(m, sig(n, 0, side, 2.0, max_hold=8), ACC(), COSTS)
        t = r.trades
        assert t["funding"][0] == pytest.approx(sign * t["notional"][0] * 0.0005)
        assert t["reason"][0] == "time"


def test_funding_outside_hold_not_charged():
    n = 20
    o, h, l, c = flat(n), [100.2] * n, [99.8] * n, flat(n)
    m0 = make_market(o, h, l, c)
    m = make_market(o, h, l, c, fund_t=[int(m0.t[15])], fund_r=[0.01])
    r = E.run_backtest(m, sig(n, 0, 1, 2.0, max_hold=5), ACC(), COSTS)
    assert r.trades["funding"][0] == 0.0


def test_missing_funding_is_assumed_cost_and_flagged():
    n = 40
    o, h, l, c = flat(n), [100.2] * n, [99.8] * n, flat(n)
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0, max_hold=30), ACC(), COSTS)
    assert r.funding_assumed and r.trades["funding"][0] < 0


def test_liquidation_loses_margin():
    n = 12
    o, h, l, c = flat(n), [100.5] * n, [99.9] * n, flat(n)
    l[2] = 99.0                                               # liq price = 100*(1-(0.01-0.004)) = 99.4
    m = make_market(o, h, l, c)
    acct = E.Account(start_equity=1000.0, risk_pct=0.01, leverage=100.0)
    r = E.run_backtest(m, sig(n, 0, 1, 5.0), acct, COSTS)     # stop 5% away - beyond liquidation
    t = r.trades
    assert t["liq"][0] == 1 and t["reason"][0] == "liquidation"
    margin = t["notional"][0] / 100.0
    assert t["net"][0] == pytest.approx(-margin - 0.001 * t["notional"][0])


def test_invalid_leverage_250x_500x_flagged():
    n = 12
    m = make_market(flat(n), [101.0] * n, [99.0] * n, flat(n))
    for L in (250, 500):
        r = E.run_backtest(m, sig(n, 0, 1, 1.0), E.Account(leverage=L), COSTS)
        assert r.invalid and len(r.trades["net"]) == 0


def test_leverage_cap_limits_notional():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.5] * n, flat(n)
    m = make_market(o, h, l, c)
    acct = E.Account(start_equity=1000.0, risk_pct=0.05, leverage=2.0)
    r = E.run_backtest(m, sig(n, 0, 1, 0.5), acct, COSTS)     # risk-sized notional would be ~5000 > 2x equity
    assert r.trades["notional"][0] <= 2000.0 + 1e-6


def test_min_notional_rejection():
    n = 12
    m = make_market(flat(n), [101.0] * n, [99.5] * n, flat(n))
    cc = E.Costs(taker_fee=0.001, slippage_bps=0, half_spread_bps=0, min_notional=10_000.0)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(), cc)
    assert r.rejected == 1 and len(r.trades["net"]) == 0


def test_cashflows_never_count_as_pnl():
    n = 30
    o, h, l, c = flat(n), [100.2] * n, [99.8] * n, flat(n)
    m = make_market(o, h, l, c)
    base = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(), COSTS)
    wd = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(cashflows=((int(m.t[10]), 500.0), (int(m.t[20]), -300.0))), COSTS)
    assert wd.trades["net"].sum() == pytest.approx(base.trades["net"].sum(), rel=1e-9)   # same trade P&L
    assert wd.net_cashflow == 200.0
    assert wd.equity_close[-1] == pytest.approx(base.equity_close[-1] + 200.0)


def test_equity_matches_ledger_and_drawdown_nonneg():
    rng = np.random.default_rng(1)
    n = 400
    c = 100 + np.cumsum(rng.normal(0, 0.5, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.2; l = np.minimum(o, c) - 0.2
    m = make_market(o, h, l, c)
    s = np.zeros(n, np.int8); s[::20] = 1
    sg = E.Signals(side=s, stop_dist=np.full(n, 1.5), tp_r=2.0, max_hold=15)
    r = E.run_backtest(m, sg, ACC(), COSTS)
    assert r.equity_close[-1] == pytest.approx(1000.0 + r.trades["net"].sum())
    assert (r.equity_worst <= r.equity_close + 1e-9).all()


def test_deterministic():
    rng = np.random.default_rng(2)
    n = 300
    c = 100 + np.cumsum(rng.normal(0, 0.5, n)); o = np.r_[c[0], c[:-1]]
    m = make_market(o, np.maximum(o, c) + .2, np.minimum(o, c) - .2, c)
    s = np.zeros(n, np.int8); s[5::13] = -1
    sg = E.Signals(side=s, stop_dist=np.full(n, 1.0), tp_r=1.5, max_hold=10)
    a = E.run_backtest(m, sg, ACC(), COSTS); b = E.run_backtest(m, sg, ACC(), COSTS)
    assert np.array_equal(a.trades["net"], b.trades["net"])


def test_no_lookahead_entry_uses_next_open():
    n = 12
    o, h, l, c = flat(n), [101.0] * n, [99.5] * n, flat(n)
    o[1] = 103.0                                              # signal at close of bar 0 must fill at bar 1 OPEN (103)
    h[1] = 104.0; c[1] = 103.5
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0), ACC(), COSTS)
    assert r.trades["entry_px"][0] == pytest.approx(103.0)


def test_drawdown_halt_and_daily_loss_halt():
    n = 300
    o = np.full(n, 100.0); h = np.full(n, 100.5); l = np.full(n, 99.5); c = np.full(n, 100.0)
    for i in range(1, n):          # every bar stops out longs
        l[i] = 97.0
    m = make_market(o, h, l, c)
    s = np.zeros(n, np.int8); s[::2] = 1
    r = E.run_backtest(m, E.Signals(side=s, stop_dist=np.full(n, 2.0), max_hold=5), E.Account(risk_pct=0.05, leverage=10), COSTS)
    assert r.halted in ("drawdown_halt", "ruined") or len(r.trades["net"]) < 100


def test_signal_exit_fills_next_open():
    n = 20
    o = [100.0] * n; h = [100.4] * n; l = [99.6] * n; c = [100.0] * n
    o[6] = 101.0
    xl = np.zeros(n, bool); xl[5] = True                     # decided at close of bar 5 -> fills at open of bar 6
    m = make_market(o, h, l, c)
    r = E.run_backtest(m, sig(n, 0, 1, 2.0, max_hold=15, exit_long=xl), ACC(), COSTS)
    assert r.trades["reason"][0] == "signal" and r.trades["exit_px"][0] == pytest.approx(101.0)
