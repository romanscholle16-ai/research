"""Performance metrics derived from engine Results (trusted; pure functions)."""
import numpy as np


def summarize(res, label="") -> dict:
    tr = res.trades
    n = len(tr["net"])
    out = {"trades": n, "invalid": res.invalid, "halted": res.halted, "rejected": res.rejected,
           "funding_assumed": bool(res.funding_assumed)}
    eq = res.equity_worst if len(res.equity_worst) else np.array([res.start_equity])
    pk = np.maximum.accumulate(eq)
    out["max_dd"] = float(((pk - eq) / np.maximum(pk, 1e-9)).max()) if len(eq) else 0.0
    out["end_equity"] = float(res.equity_close[-1]) if len(res.equity_close) else res.start_equity
    if n == 0:
        out.update(gross=0.0, net=0.0, fees=0.0, funding=0.0, slippage=0.0, expectancy_r=0.0, exp_usd=0.0, t_stat=0.0, pf=0.0,
                   win_rate=0.0, avg_hold_bars=0.0, liquidations=0, exposure=0.0, turnover=0.0, sd_r=0.0, sharpe=0.0)
        return out
    net, r = tr["net"].astype(float), tr["r"].astype(float)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    win, loss = net[net > 0].sum(), -net[net < 0].sum()
    hold = (tr["exit_i"] - tr["entry_i"]).astype(float)
    ret = np.diff(np.concatenate(([res.start_equity], res.equity_close)))
    dpb = max(1, int(round(86_400_000 / max(1, np.median(np.diff(res.t))))) ) if len(res.t) > 1 else 24
    k = len(ret) // dpb
    daily = ret[:k * dpb].reshape(k, dpb).sum(1) / res.start_equity if k > 1 else np.zeros(1)
    out.update(
        gross=float(tr["gross"].sum()), net=float(net.sum()), fees=float(tr["fees"].sum()),
        funding=float(tr["funding"].sum()), slippage=float(tr["slip"].sum()),
        expectancy_r=float(r.mean()), exp_usd=float(net.mean()), sd_r=sd,
        t_stat=float(r.mean() / (sd / np.sqrt(n))) if sd > 0 else 0.0,
        pf=float(win / loss) if loss > 0 else (float("inf") if win > 0 else 0.0),
        win_rate=float((net > 0).mean()), avg_hold_bars=float(hold.mean()), liquidations=int(tr["liq"].sum()),
        exposure=float(hold.sum() / max(1, res.n)), turnover=float(tr["notional"].sum() * 2 / res.start_equity),
        sharpe=float(daily.mean() / daily.std() * np.sqrt(365)) if daily.std() > 0 else 0.0)
    if out["pf"] == float("inf"):
        out["pf"] = 99.0
    return out


def block_bootstrap_mean(x, n_boot=2000, level=0.95, seed=1):
    """Stationary-ish block bootstrap CI of the mean (blocks preserve serial dependence between trades)."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    b = max(1, int(round(n ** (1 / 3))))
    nb = int(np.ceil(n / b))
    starts = rng.integers(0, n - b + 1, size=(n_boot, nb))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = x[idx].mean(1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def monte_carlo(r, risk_pct, start, paths, horizon_trades, ruin_frac, seed=7, block=None):
    """Resample trade R-multiples (block bootstrap) at fixed fractional risk with compounding.
    Returns drawdown distribution and probability equity touches ruin_frac*start."""
    r = np.asarray(r, float)
    n = len(r)
    if n < 10:
        return {"p_ruin": 1.0, "dd_p50": 1.0, "dd_p95": 1.0, "end_p05": 0.0, "end_p50": 0.0}
    rng = np.random.default_rng(seed)
    b = block or max(1, int(round(n ** (1 / 3))))
    nb = int(np.ceil(horizon_trades / b))
    starts = rng.integers(0, n - b + 1, size=(paths, nb))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(paths, -1)[:, :horizon_trades]
    mult = np.maximum(1 + risk_pct * r[idx], 0.0)
    eq = start * np.cumprod(mult, axis=1)
    pk = np.maximum.accumulate(np.concatenate([np.full((paths, 1), start), eq], axis=1), axis=1)[:, 1:]
    dd = ((pk - eq) / pk).max(1)
    ruin = (eq.min(1) <= ruin_frac * start)
    return {"p_ruin": float(ruin.mean()), "dd_p50": float(np.quantile(dd, .5)), "dd_p95": float(np.quantile(dd, .95)),
            "end_p05": float(np.quantile(eq[:, -1], .05)), "end_p50": float(np.quantile(eq[:, -1], .5)),
            "end_p95": float(np.quantile(eq[:, -1], .95))}


def monthly_pnl(t_ms, net, months_span=None):
    """Aggregate trade net P&L by calendar month (exit timestamp). Returns array of monthly sums incl. empty months."""
    if len(net) == 0:
        return np.zeros(0)
    mo = (np.asarray(t_ms, np.int64) // (30 * 86_400_000))
    lo, hi = mo.min(), mo.max()
    out = np.zeros(hi - lo + 1)
    np.add.at(out, mo - lo, net)
    return out
