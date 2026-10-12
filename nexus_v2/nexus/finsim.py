"""Financial simulator: $300 simulated equity, compounding, costs, withdrawal policy, risk of ruin, uncertainty.
Results are SIMULATIONS from stated assumptions or stored candidate trade statistics - never income, never a forecast.
Deposits and withdrawals are tracked separately from trading P&L."""
import numpy as np
from . import config

C = config.CAPITAL


def simulate(r_samples=None, hypothetical=None, risk_pct=None, start=None, months=6, paths=2000, trades_per_month=None,
             cost_drag_r=0.0, monthly_withdrawal_target=None, deposits_per_month=0.0, seed=1, ruin_frac=None):
    start = float(start if start is not None else C["start_equity"])
    risk_pct = float(risk_pct if risk_pct is not None else config.RISK["risk_per_trade"])
    ruin_frac = ruin_frac if ruin_frac is not None else config.THRESHOLDS["ruin_equity_fraction"]
    rng = np.random.default_rng(seed)
    if r_samples is not None and len(r_samples) >= 10:
        basis = f"bootstrap of {len(r_samples)} stored candidate trades (R-multiples, net of modeled costs)"
        src = np.asarray(r_samples, float)
        draw = lambda k: rng.choice(src, size=k, replace=True)
    else:
        h = hypothetical or {"win_rate": 0.4, "avg_win_r": 2.0, "avg_loss_r": 1.0}
        basis = (f"HYPOTHETICAL: win rate {h['win_rate']:.0%}, avg win {h['avg_win_r']}R, avg loss {h['avg_loss_r']}R "
                 f"(not evidence about any strategy)")
        draw = lambda k: np.where(rng.random(k) < h["win_rate"], h["avg_win_r"], -h["avg_loss_r"])
    tpm = float(trades_per_month or 20)
    eq = np.full(paths, start)
    peak = eq.copy()
    min_eq = eq.copy()
    maxdd = np.zeros(paths)
    ruined = np.zeros(paths, bool)
    profits, equities, withdrawn_tot = [], [], np.zeros(paths)
    for mth in range(months):
        eq_start = eq.copy()
        ntr = rng.poisson(tpm, size=paths)
        kmax = int(ntr.max()) if paths else 0
        for k in range(kmax):
            act = (ntr > k) & ~ruined
            if not act.any():
                break
            rr = draw(paths) - cost_drag_r
            eq = np.where(act, eq * np.maximum(1 + risk_pct * rr, 0.0), eq)
            peak = np.maximum(peak, eq)
            maxdd = np.maximum(maxdd, (peak - eq) / peak)
            min_eq = np.minimum(min_eq, eq)
            ruined |= eq <= ruin_frac * start
        profit = eq - eq_start                                     # trading P&L only
        profits.append(profit.copy())
        w = np.zeros(paths)
        if monthly_withdrawal_target:
            w = np.minimum(monthly_withdrawal_target, C["max_withdrawal_fraction"] * np.maximum(profit, 0.0))   # <=50% of positive profit
            eq = eq - w
        eq = eq + deposits_per_month                                  # deposits never counted as profit
        withdrawn_tot += w
        equities.append(eq.copy())
    P = np.array(profits)
    q = lambda a, p: [float(np.quantile(a, p)) for p in p]
    pct = [0.05, 0.25, 0.5, 0.75, 0.95]
    need_ret = C["min_monthly_net_profit"] / start
    return {
        "basis": basis, "start_equity": start, "months": months, "paths": paths, "risk_pct": risk_pct, "trades_per_month": tpm,
        "monthly_profit_quantiles": [{"month": m + 1, "p05": q(P[m], [0.05])[0], "p25": q(P[m], [0.25])[0], "p50": q(P[m], [0.5])[0],
                                      "p75": q(P[m], [0.75])[0], "p95": q(P[m], [0.95])[0]} for m in range(months)],
        "equity_quantiles": [{"month": m + 1, "p05": q(equities[m], [0.05])[0], "p50": q(equities[m], [0.5])[0], "p95": q(equities[m], [0.95])[0]}
                             for m in range(months)],
        "prob_ruin": float(ruined.mean()), "max_drawdown_p50": float(np.median(maxdd)), "max_drawdown_p95": float(np.quantile(maxdd, 0.95)),
        "prob_month_ge_min_profit": float((P >= C["min_monthly_net_profit"]).mean()),
        "prob_month_ge_stretch_profit": float((P >= C["stretch_monthly_net_profit"]).mean()),
        "avg_withdrawn_total_p50": float(np.median(withdrawn_tot)),
        "required_monthly_return_for_min_profit": need_ret,
        "required_monthly_return_for_stretch_profit": C["stretch_monthly_net_profit"] / start,
        "retention_check": "withdrawals capped at 50% of each positive month's trading profit; deposits excluded from profit",
        "caveats": ["Bootstrap assumes future trades resemble past ones; regime change invalidates it.",
                    "Fixed fractional risk with compounding; exchange minimum sizes and liquidity limits at larger equity are not modelled.",
                    "Simulated outcomes are not real income and not a prediction."],
    }
