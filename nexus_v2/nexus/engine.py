"""Trusted event-driven backtester (isolated-margin USDT-margined linear perpetual).

Timing contract
  * A signal at index i is decided at the CLOSE of bar i using data <= i only.
  * The entry fills at the OPEN of bar i+1 (adverse slippage + half spread).
  * Stops/targets are evaluated on bars from the entry bar onward. If a stop and a target are both
    reachable inside one bar the STOP is assumed to fill first (conservative). Gaps through a stop
    fill at the (worse) open. Targets never receive price improvement.
  * Signal exits decided at the close of bar k fill at the open of bar k+1.
Accounting
  * net = gross - entry/exit fees - slippage/spread - funding (- liquidation penalty if liquidated).
  * Deposits / withdrawals are a separate ledger and never appear in trading P&L.
This file is part of the trusted computing base: research agents must not modify it (see Guardian 3).
"""
from dataclasses import dataclass, field
import numpy as np

from . import config


@dataclass
class Signals:
    side: np.ndarray                      # int8 per bar: +1 long, -1 short, 0 none (decided at bar close)
    stop_dist: np.ndarray                 # absolute price distance of the initial stop, known at decision time
    tp_r: float = 0.0                     # take-profit in multiples of initial stop distance (0 = none)
    max_hold: int = 48                    # bars
    exit_long: np.ndarray = None          # bool: close longs at next open
    exit_short: np.ndarray = None


@dataclass
class Account:
    start_equity: float = 300.0
    risk_pct: float = 0.01
    leverage: float = 10.0
    compound: bool = True
    daily_loss_halt: float = 0.05
    drawdown_halt: float = 0.40
    cashflows: tuple = ()                 # ((epoch_ms, amount), ...) deposits(+)/withdrawals(-), NOT trading P&L


@dataclass
class Costs:
    taker_fee: float = 0.0006
    slippage_bps: float = 2.0
    half_spread_bps: float = 0.5
    funding_assumed_rate_8h: float = 0.0001
    mmr: float = 0.004
    min_notional: float = 5.0
    qty_step: float = 0.001
    tp_is_taker: bool = True
    funding_mult: float = 1.0

    @classmethod
    def default(cls, **over):
        d = dict(config.COSTS)
        d.pop("tp_is_taker", None)
        d.update(over)
        return cls(**d)

    def stressed(self, mult, funding_mult=1.0):
        return Costs(self.taker_fee * mult, self.slippage_bps * mult, self.half_spread_bps * mult,
                     self.funding_assumed_rate_8h, self.mmr, self.min_notional, self.qty_step, self.tp_is_taker, funding_mult)


class InvalidLeverage(ValueError):
    pass


@dataclass
class Result:
    trades: dict
    equity_close: np.ndarray
    equity_worst: np.ndarray
    t: np.ndarray
    start_equity: float
    net_cashflow: float
    rejected: int = 0
    halted: str = ""
    invalid: str = ""
    funding_assumed: bool = False
    n: int = 0
    meta: dict = field(default_factory=dict)


_TCOLS = ("entry_i", "exit_i", "side", "entry_px", "exit_px", "qty", "notional", "gross", "fees", "slip", "funding",
          "net", "r", "liq", "reason")


def _empty_trades():
    return {k: np.zeros(0) for k in _TCOLS}


def run_backtest(m, sig: Signals, acct: Account, costs: Costs, i0=0, i1=None) -> Result:
    """Simulate on bars [i0,i1) of market m. Signals must be causal (computed on a prefix)."""
    n_all = len(m.t)
    i1 = n_all if i1 is None else min(i1, n_all)
    side, sd = sig.side, sig.stop_dist
    L = float(acct.leverage)
    n = max(0, i1 - i0)
    tt = m.t
    if 1.0 / L <= costs.mmr + 2 * costs.taker_fee:
        # initial margin cannot cover maintenance margin plus fees -> simulation assumptions are invalid
        flat = np.full(n, acct.start_equity)
        return Result(_empty_trades(), flat, flat.copy(), tt[i0:i1], acct.start_equity, 0.0,
                      invalid=f"leverage {L:g}x invalid: 1/L={1/L:.4%} <= mmr+2*fee={costs.mmr + 2*costs.taker_fee:.4%}", n=n)
    slip = (costs.slippage_bps + costs.half_spread_bps) / 1e4
    fee = costs.taker_fee
    d_trade = np.zeros(n)
    cf = np.zeros(n)
    for (ts, amt) in acct.cashflows:
        j = int(np.searchsorted(tt[i0:i1], ts))
        if j < n:
            cf[j] += amt
    cf_cum = np.cumsum(cf)
    unr_c, unr_w = np.zeros(n), np.zeros(n)
    cols = {k: [] for k in _TCOLS}
    realized, peak, rej, halted = 0.0, acct.start_equity, 0, ""
    day_key, day_start_eq, day_pnl = None, acct.start_equity, 0.0
    ft, fr = m.fund_t, m.fund_r
    has_funding = len(ft) > 0
    cand = (np.flatnonzero(side[i0:i1 - 1]) + i0) if n > 1 else np.zeros(0, int)
    next_ok = i0
    for j in cand:
        j = int(j)
        if j < next_ok:
            continue
        s, e, sdist = int(side[j]), j + 1, float(sd[j])
        if not np.isfinite(sdist) or sdist <= 0:
            continue
        # ---- account gates (use only realised state at decision time) --------------------------
        equity = acct.start_equity + realized + cf_cum[j - i0] if acct.compound else acct.start_equity
        if equity <= acct.start_equity * 0.02:
            halted = "ruined"; break
        peak = max(peak, equity)
        if (peak - equity) / peak >= acct.drawdown_halt:
            halted = "drawdown_halt"; break
        dk = int(tt[e] // 86_400_000)
        if dk != day_key:
            day_key, day_start_eq, day_pnl = dk, equity, 0.0
        if day_pnl <= -acct.daily_loss_halt * day_start_eq:
            continue
        # ---- entry sizing ----------------------------------------------------------------------
        open_e = float(m.o[e])
        px = open_e * (1 + s * slip)
        stop = px - s * sdist
        if stop <= 0:
            continue
        risk_usd = acct.risk_pct * equity
        notional = min(risk_usd / (sdist / px + 2 * fee + 2 * slip), equity * L)
        qty = np.floor(notional / px / costs.qty_step + 1e-12) * costs.qty_step
        notional = qty * px
        if qty <= 0 or notional < costs.min_notional or notional / L > equity:
            rej += 1
            continue
        margin = notional / L
        liq = px * (1 - s * (1.0 / L - costs.mmr))
        tp = px + s * sig.tp_r * sdist if sig.tp_r > 0 else None
        end = min(e + sig.max_hold - 1, i1 - 1)
        K = end - e + 1
        lo, hi, op = m.l[e:end + 1], m.h[e:end + 1], m.o[e:end + 1]
        BIG = K + 10
        if s == 1:
            eff, liq_first = max(stop, liq), liq > stop
            sh = np.flatnonzero(lo <= eff)
            th = np.flatnonzero(hi >= tp) if tp is not None else ()
        else:
            eff, liq_first = min(stop, liq), liq < stop
            sh = np.flatnonzero(hi >= eff)
            th = np.flatnonzero(lo <= tp) if tp is not None else ()
        k_stop = int(sh[0]) if len(sh) else BIG
        k_tp = int(th[0]) if len(th) else BIG
        ex_arr = sig.exit_long if s == 1 else sig.exit_short
        k_ex = BIG
        if ex_arr is not None:
            xs = np.flatnonzero(ex_arr[e:end + 1])
            if len(xs):
                k_ex = int(xs[0]) + 1          # decided at close of rel. bar x -> fills at open of x+1
        k_intra = min(k_stop, k_tp)
        liquidated = False
        xraw, slip_x = None, slip
        if k_ex <= K and k_ex <= k_intra:      # open-fill precedes intrabar events of the same bar
            kx, reason = k_ex, "signal"
        elif k_intra < K:
            kx = k_intra
            if k_stop <= k_tp:                 # stop wins ties (conservative)
                if liq_first:
                    reason, liquidated, xraw, slip_x = "liquidation", True, liq, 0.0
                else:
                    gap = (op[kx] <= stop) if s == 1 else (op[kx] >= stop)
                    reason, xraw = "stop", (float(op[kx]) if (gap and kx > 0) else stop)
            else:
                reason, xraw, slip_x = "target", tp, 0.0
        else:
            kx, reason = K, "time"
        if reason in ("signal", "time"):
            if e + kx < i1:
                xbar, xraw = e + kx, float(m.o[e + kx])
            else:
                xbar, xraw, reason = i1 - 1, float(m.c[i1 - 1]), "end_of_data"
            exit_ms = int(tt[xbar]) if reason != "end_of_data" else int(tt[xbar]) + m.tf_ms - 1
        else:
            xbar = e + kx
            exit_ms = int(tt[xbar]) + m.tf_ms - 1    # intrabar: assume the latest possible time (more funding)
        xpx = xraw * (1 - s * slip_x)
        qty_f = float(qty)
        gross = s * (xraw - open_e) * qty_f
        slipcost = gross - s * (xpx - px) * qty_f
        fees = fee * notional + fee * qty_f * xpx
        if has_funding:
            a, b = np.searchsorted(ft, tt[e], side="right"), np.searchsorted(ft, exit_ms, side="right")
            fund = -s * notional * float(fr[a:b].sum()) * costs.funding_mult
        else:  # no history: assume a COST (never a credit) and flag the run as non-qualifying
            fund = -notional * max(0, (exit_ms - int(tt[e])) // 28_800_000) * costs.funding_assumed_rate_8h * costs.funding_mult
        net = gross - slipcost - fees + fund
        if liquidated:
            net = -margin - fee * notional + fund
        # ---- equity path ------------------------------------------------------------------------
        if xbar > e:
            ks = np.arange(e, xbar)
            unr_c[ks - i0] += s * (m.c[ks] - px) * qty_f - fee * notional
            worst = m.l[ks] if s == 1 else m.h[ks]
            unr_w[ks - i0] += s * (worst - px) * qty_f - fee * notional
        d_trade[xbar - i0] += net
        realized += net
        day_pnl += net
        for kk, vv in zip(_TCOLS, (e, xbar, s, px, xpx, qty_f, notional, gross, fees, slipcost, fund, net,
                                   net / risk_usd, int(liquidated), reason)):
            cols[kk].append(vv)
        next_ok = max(xbar, e + 1)
    tr = _empty_trades() if not cols["net"] else {k: np.array(v, dtype=object if k == "reason" else float)
                                                   for k, v in cols.items()}
    base = acct.start_equity + np.cumsum(d_trade) + cf_cum
    return Result(tr, base + unr_c, base + unr_w, tt[i0:i1], acct.start_equity, float(cf.sum()), rejected=rej,
                  halted=halted, funding_assumed=not has_funding, n=n)
