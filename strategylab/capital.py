"""$300 capital-growth engine: sizing, leverage, compounding, fees (already in trade net), funding, liquidation,
monthly accounting and withdrawals. Vectorised month-block bootstrap + historical rolling windows.

Withdrawal policy: at each month end, profit_m = equity_end - equity_start. If profit_m > 0 the account may
withdraw at most (1 - retain_frac) of it, so >= retain_frac of every positive month is retained. "Capacity"
= that maximum. Actual withdrawal = min(capacity, withdraw_target). Probabilities of reaching $1k/$2k/$3k
monthly withdrawals are computed on capacity (what the account COULD withdraw while keeping 50%).
"""
from __future__ import annotations
import math
import numpy as np
from .backtest import Trades
from .btcc import ExchangeModel, DEFAULT

THRESH_EQ = (1000, 3000, 5000, 10000)
THRESH_WD = (1000, 2000, 3000)
RUIN_FLOOR = 15.0


def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 1.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - w), min(1.0, c + w))


def month_blocks(tr: Trades, t0: float, t1: float):
    """Split trades (by exit time = when pnl is realised) into calendar-month blocks, incl. empty months."""
    import datetime as dt
    def key(t): d = dt.datetime.fromtimestamp(t, dt.timezone.utc); return d.year * 12 + d.month - 1
    k0, k1 = key(t0), key(t1 - 1)
    keys = np.array([key(t) for t in tr.t_out]) if len(tr) else np.array([], int)
    blocks = []
    for k in range(k0, k1 + 1):
        m = keys == k
        blocks.append({f: getattr(tr, f)[m] for f in ("net", "stop_frac", "mae", "entry", "bars", "scale")} if len(tr) else
                      {f: np.array([]) for f in ("net", "stop_frac", "mae", "entry", "bars", "scale")})
        blocks[-1]["net"] = (tr.net[m] if len(tr) else np.array([]))
    return blocks


def _pad(blocks):
    K = max(1, max(len(b["net"]) for b in blocks))
    fl = ("net", "stop_frac", "mae", "entry", "scale")
    M = {f: np.zeros((len(blocks), K)) for f in fl}; mask = np.zeros((len(blocks), K), bool)
    for i, b in enumerate(blocks):
        k = len(b["net"]); mask[i, :k] = True
        for f in fl: M[f][i, :k] = b[f]
    return M, mask


def simulate(blocks, idx, symbol, risk_frac, leverage, em: ExchangeModel = DEFAULT, margin="isolated",
             start=300.0, retain=0.5, wd_target=1000.0, withdraw=True):
    """idx: int array [paths, months] of block indices. Returns per-path arrays."""
    M, mask = _pad(blocks)
    P, T = idx.shape
    eq = np.full(P, start); wd_total = np.zeros(P); minw = np.full(P, start); peakw = np.full(P, start)
    cap = np.zeros((P, T)); eqm = np.zeros((P, T)); liqs = np.zeros(P); ntr = np.zeros((P, T))
    step_qty = {"BTCUSDT": 0.001, "ETHUSDT": 0.01}.get(symbol, 0.01); minq = em.minq(symbol)
    fee_buf = 2 * em.taker_fee
    for t in range(T):
        sel = idx[:, t]; eq0 = eq.copy()
        for k in range(M["net"].shape[1]):
            valid = mask[sel, k]
            if not valid.any(): continue
            net, sf, mae, px, sc = (M[f][sel, k] for f in ("net", "stop_frac", "mae", "entry", "scale"))
            sc = np.where(sc > 0, sc, 1.0)
            notional = (risk_frac * sc * eq) / (sf + fee_buf)
            maxn = leverage * eq * 0.95
            notional = np.minimum(notional, maxn)
            px = np.where(px > 0, px, 1.0)
            qty = np.floor(notional / px / step_qty) * step_qty
            qty = np.where((qty < minq) & (maxn / px >= minq), minq, qty)    # round up to min lot if margin allows
            ok = valid & (qty >= minq) & (qty * px <= maxn + 1e-9) & (eq > RUIN_FLOOR)
            notional = qty * px
            liq_d = (1.0 / leverage - em.maint_margin_rate) if margin == "isolated" else (eq / np.maximum(notional, 1e-9) - em.maint_margin_rate)
            liquidated = ok & (mae >= liq_d)
            margin_amt = np.minimum(notional / leverage, eq) if margin == "isolated" else eq
            pnl = np.where(liquidated, -margin_amt, notional * net)
            pnl = np.where(ok, pnl, 0.0)
            eq = np.maximum(eq + pnl, 0.0); liqs += liquidated; ntr[:, t] += ok
            minw = np.minimum(minw, eq + wd_total)
            peakw = np.maximum(peakw, eq + wd_total)
        profit = eq - eq0
        cap[:, t] = np.where(profit > 0, (1 - retain) * profit, 0.0)
        if withdraw:
            w = np.minimum(cap[:, t], wd_target); eq = eq - w; wd_total += w
        eqm[:, t] = eq
    return dict(final_eq=eq, wd_total=wd_total, wealth=eq + wd_total, minw=minw, peakw=peakw, cap=cap, eqm=eqm, liqs=liqs, ntr=ntr)


def ledger(tr: Trades, symbol, risk_frac, leverage, em: ExchangeModel = DEFAULT, margin="isolated", start=300.0, retain=0.5, wd_target=1000.0, t0=None, t1=None):
    """Independent, scalar, chronological account ledger (one path = the realised trade sequence). Keeps the five quantities apart, per trade:
       underlying move  -> `ret` (price return of the position, per unit notional, net of fees/funding = `net`)
       margin return    -> net * leverage_used   (return on the margin locked in the position; can be +400%)
       dollar P&L       -> notional * net        (what actually changes the balance)
       account return   -> dollar P&L / equity before the trade
       withdrawable     -> (1-retain) x positive monthly profit, capped at wd_target, taken at month end.
    Same sizing, rounding, liquidation and withdrawal rules as `simulate`; used to audit it."""
    import datetime as dt
    rows, eq, wd_total = [], start, 0.0
    step = {"BTCUSDT": 0.001, "ETHUSDT": 0.01}.get(symbol, 0.01); minq = em.minq(symbol); fee_buf = 2 * em.taker_fee
    order = np.argsort(tr.t_out) if len(tr) else []
    mkey = lambda t: (lambda d: d.year * 12 + d.month - 1)(dt.datetime.fromtimestamp(t, dt.timezone.utc))
    cur, eq_m0, months = None, eq, []
    for i in order:
        k = mkey(tr.t_out[i])
        if cur is None: cur = k
        while k > cur:                                   # month boundary: withdraw (1-retain) of the positive profit, capped
            profit = eq - eq_m0; w = min((1 - retain) * profit, wd_target) if profit > 0 else 0.0
            eq -= w; wd_total += w; months.append(dict(month=cur, profit=profit, withdrawn=w, equity=eq)); cur += 1; eq_m0 = eq
        sf, net, mae, px, sc = tr.stop_frac[i], tr.net[i], tr.mae[i], tr.entry[i], (tr.scale[i] if tr.scale[i] > 0 else 1.0)
        notional = min(risk_frac * sc * eq / (sf + fee_buf), leverage * eq * 0.95)
        qty = np.floor(notional / px / step) * step
        maxn = leverage * eq * 0.95
        if qty < minq and maxn / px >= minq: qty = minq
        if qty < minq or qty * px > maxn + 1e-9 or eq <= RUIN_FLOOR: continue
        notional = qty * px; marg = min(notional / leverage, eq)
        liq_d = (1.0 / leverage - em.maint_margin_rate) if margin == "isolated" else (eq / max(notional, 1e-9) - em.maint_margin_rate)
        liquidated = mae >= liq_d
        pnl = -marg if liquidated else notional * net
        rows.append(dict(t=float(tr.t_out[i]), equity_before=eq, notional=notional, margin=marg, price_move=float(tr.ret[i]), net_price_return=float(net),
                         margin_return=pnl / marg if marg > 0 else 0.0, pnl=pnl, account_return=pnl / eq if eq > 0 else 0.0, liquidated=bool(liquidated)))
        eq = max(eq + pnl, 0.0)
    if cur is not None:
        profit = eq - eq_m0; w = min((1 - retain) * profit, wd_target) if profit > 0 else 0.0
        eq -= w; wd_total += w; months.append(dict(month=cur, profit=profit, withdrawn=w, equity=eq))
    return dict(trades=rows, months=months, final_equity=eq, withdrawn_total=wd_total)


def summarize(sim, start=300.0, wd_target=1000.0):
    P, T = sim["cap"].shape
    def prob(mask):
        k = int(np.sum(mask)); lo, hi = wilson(k, P); return dict(p=k / P, lo=lo, hi=hi)
    out = {"paths": P, "months": T}
    out["reach_equity"] = {str(x): prob(sim["peakw"] >= x) for x in THRESH_EQ}
    out["monthly_withdraw_any"] = {str(x): prob((sim["cap"] >= x).any(axis=1)) for x in THRESH_WD}
    last3 = sim["cap"][:, -3:] if T >= 3 else sim["cap"]
    out["sustained_withdraw_target"] = prob((last3 >= wd_target).all(axis=1) & (sim["final_eq"] > RUIN_FLOOR))
    for pct in (25, 50, 75):
        out[f"lose_{pct}pct"] = prob(sim["minw"] <= start * (1 - pct / 100))
    out["ruin"] = prob((sim["minw"] <= RUIN_FLOOR) | (sim["final_eq"] <= RUIN_FLOOR))
    out["any_liquidation"] = prob(sim["liqs"] > 0)
    w = sim["wealth"]
    out["final_wealth_pct"] = {str(q): float(np.percentile(w, q)) for q in (5, 25, 50, 75, 95)}
    out["median_monthly_capacity"] = float(np.median(sim["cap"]))
    out["equity_band"] = {str(q): np.percentile(sim["eqm"], q, axis=0).round(2).tolist() for q in (5, 25, 50, 75, 95)}
    out["median_trades_per_month"] = float(np.median(sim["ntr"].mean(axis=1)))
    return out


def evaluate(tr: Trades, t0, t1, symbol, risk_frac, leverage, em=DEFAULT, margin="isolated", paths=2000, months=6, seed=1,
             start=300.0, retain=0.5, wd_target=1000.0):
    blocks = month_blocks(tr, t0, t1)
    if len(blocks) < 2: return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(blocks), size=(paths, months))
    res = summarize(simulate(blocks, idx, symbol, risk_frac, leverage, em, margin, start, retain, wd_target), start, wd_target)
    # historical rolling windows (stride 1 month) - real sequences, not resampled
    if len(blocks) >= months:
        w = np.array([list(range(s, s + months)) for s in range(len(blocks) - months + 1)])
        hs = summarize(simulate(blocks, w, symbol, risk_frac, leverage, em, margin, start, retain, wd_target), start, wd_target)
        res["historical_windows"] = dict(n=len(w), reach_withdraw_1000=hs["monthly_withdraw_any"]["1000"]["p"],
                                          ruin=hs["ruin"]["p"], median_final_wealth=hs["final_wealth_pct"]["50"], median_equity_band=hs["equity_band"]["50"])
    res["sizing"] = dict(risk_frac=risk_frac, leverage=leverage, margin=margin)
    lg = ledger(tr, symbol, risk_frac, leverage, em, margin, start, retain, wd_target)["trades"]       # audit view: underlying move vs margin return vs dollars vs account %
    if lg:
        w = [x for x in lg if x["pnl"] > 0]
        res["accounting"] = dict(trades=len(lg), median_margin_frac_of_equity=float(np.median([x["margin"] / x["equity_before"] for x in lg])),
                                 median_price_move_win=float(np.median([x["price_move"] for x in w])) if w else None,
                                 median_margin_return_win=float(np.median([x["margin_return"] for x in w])) if w else None,
                                 median_dollar_win=float(np.median([x["pnl"] for x in w])) if w else None,
                                 median_account_return_per_trade=float(np.median([x["account_return"] for x in lg])), liquidations=int(sum(x["liquidated"] for x in lg)))
    res["assumption"] = ("Month-block bootstrap of realised trade P&L; assumes future trade distribution equals sampled history. "
                         "Not a forecast. Costs/slippage per ExchangeModel (unverified vs BTCC).")
    return res


RISK_GRID = [0.01, 0.02, 0.03, 0.05, 0.08, 0.12]
LEV_GRID = [5, 10, 20, 50]
HI_RISK_GRID = [0.05, 0.10, 0.20, 0.30, 0.50]      # high-leverage mode (user accepts higher ruin risk)
HI_LEV_GRID = [250, 500]                            # the strategy must work at 250x or 500x


def optimize_risk(tr, t0, t1, symbol, em=DEFAULT, max_ruin=0.05, paths=600, seed=7, margins=("isolated", "cross"), max_lose50=0.35):
    """Pick (risk_frac, leverage, margin) maximising P(monthly capacity >= $1000 in horizon) s.t. ruin<=max_ruin and P(lose50%)<=0.35.
    Fitted ONLY on the trades/period passed in (never on the locked test period)."""
    best = None; table = []
    blocks = month_blocks(tr, t0, t1)
    if len(blocks) < 2: return None, table
    rng = np.random.default_rng(seed); idx = rng.integers(0, len(blocks), size=(paths, 6))
    for m in margins:
        for r in (HI_RISK_GRID if em.high_leverage else RISK_GRID):
            for L in ([l for l in HI_LEV_GRID if l <= em.max_leverage] if em.high_leverage else LEV_GRID):
                s = summarize(simulate(blocks, idx, symbol, r, L, em, m), 300.0, 1000.0)
                row = dict(risk=r, lev=L, margin=m, p1000=s["monthly_withdraw_any"]["1000"]["p"], ruin=s["ruin"]["p"],
                           lose50=s["lose_50pct"]["p"], med=s["final_wealth_pct"]["50"])
                table.append(row)
                feas = row["ruin"] <= max_ruin and row["lose50"] <= max_lose50
                key = (feas, row["p1000"], row["med"])
                if best is None or key > best[0]: best = (key, row)
    return best[1], table
