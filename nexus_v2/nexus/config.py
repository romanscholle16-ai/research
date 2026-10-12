"""Trusted configuration. Goals, thresholds, risk limits and cost model live here and are hashed.

Agents cannot modify these: Guardian 3 compares the live hash with the baseline stored at first
initialisation and raises a critical incident on any difference. Mutable operational settings
(worker count, budgets) are separate and range-validated (see SAFE_SETTINGS).
"""
import hashlib, json, os, pathlib

CONFIG_VERSION = "2.0.0"


def home() -> pathlib.Path:
    p = os.environ.get("NEXUS_HOME")
    base = pathlib.Path(p) if p else pathlib.Path(__file__).resolve().parents[1] / "var"
    base.mkdir(parents=True, exist_ok=True)
    return base


def path(*parts, mkdir=True) -> pathlib.Path:
    """Path under the data home. Only PARENT directories are created, so the last part may be a file or a dir."""
    p = home().joinpath(*parts)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> pathlib.Path:
    return path("nexus.db")


# ----------------------------------------------------------------------------------------------
# The 15 financial goals (fixed; names must never be abbreviated in the UI)
# ----------------------------------------------------------------------------------------------
GOALS = [
    (1, "Starting Capital", "$300 USD (simulated)"),
    (2, "BTCC Compatibility", "Verified instrument and trading assumptions"),
    (3, "250x Leverage", "Verified availability and valid simulation assumptions"),
    (4, "500x Leverage", "Verified availability and valid simulation assumptions"),
    (5, "Trading Frequency", ">=1 completed simulated trade per rolling 7 days (30-day activity shown)"),
    (6, "Positive Net Expectancy", "Positive expectancy after modeled costs with sufficient independent validation"),
    (7, "Minimum Monthly Net Profit", "$2,000 USD"),
    (8, "Stretch Monthly Net Profit", "$6,000 USD"),
    (9, "Minimum Monthly Withdrawal", "$1,000 USD"),
    (10, "Stretch Monthly Withdrawal", "$3,000 USD"),
    (11, "Profit Retention", "Retain >=50% of positive monthly net profit when withdrawing"),
    (12, "Six-Month Objective", "Evidence toward the objective within 6 months of project start"),
    (13, "Drawdown and Risk of Ruin", "Defined risk limits are defined, enforced and satisfied"),
    (14, "Independent Validation", "Passed the independent validation protocol"),
    (15, "Protected Holdout and Paper-Forward Qualification", "Passed protected holdout and paper-forward gates"),
]

CAPITAL = {
    "start_equity": 300.0,
    "min_monthly_net_profit": 2000.0,
    "stretch_monthly_net_profit": 6000.0,
    "min_monthly_withdrawal": 1000.0,
    "stretch_monthly_withdrawal": 3000.0,
    "max_withdrawal_fraction": 0.5,
    "target_leverages": [250, 500],
    "project_horizon_months": 6,
}

# ----------------------------------------------------------------------------------------------
# Acceptance thresholds, each with its statistical rationale
# ----------------------------------------------------------------------------------------------
THRESHOLDS = {
    # stage 1 / 2 (exploratory + development screen)
    "s1_min_trades": 10,
    "s2_min_trades": 80,           # SE of mean R ~ sd/sqrt(n); below ~80 trades a 1.5-sigma edge is not distinguishable
    "s2_min_t": 1.5,
    "s2_min_pf": 1.05,
    # stage 3 walk-forward (out-of-sample relative to parameter selection)
    "wf_folds": 4,
    "wf_min_trades": 60,
    "wf_min_positive_fold_frac": 0.6,
    "wf_ci_level": 0.90,           # two-sided 90% block-bootstrap CI (= one-sided 95% lower bound) of mean R must exceed 0
    "wf_min_multiple_testing_t": 1.2,   # floor; actual bar = max(floor, sqrt(2 ln M)) with M = candidates tested in WF
    # stage 4 robustness
    "rb_min_total_trades": 120,
    "rb_min_neighbor_positive_frac": 0.6,
    "rb_min_neighbors": 4,
    "rb_cost_stress_multiplier": 2.0,
    "rb_funding_stress_multiplier": 3.0,
    "rb_outlier_trim": 3,          # exp stays > 0 after removing the 3 best trades
    "rb_min_regimes_nonneg": 2,    # of 3 volatility regimes
    "rb_mc_paths": 2000,
    "max_drawdown": 0.35,          # 95th pct of MC max drawdown must be <= 35% of equity
    "ruin_equity_fraction": 0.5,   # "ruin" = equity falls to 50% of start (or liquidation) in the horizon
    "max_risk_of_ruin": 0.05,
    # stage 5 protected holdout
    "ho_min_trades": 30,
    "ho_min_pf": 1.0,
    "ho_max_evaluations_total": 12,   # holdout budget: each look at the holdout is a selection event
    # stage 6 paper-forward
    "pf_min_days": 30,
    "pf_min_trades": 10,
    "pf_min_pf": 1.0,
    # trading frequency goal
    "freq_min_rolling7_coverage": 0.9,
    # data quality
    "max_gap_frac": 0.005,
}

RISK = {
    "risk_per_trade": 0.01,         # fraction of current equity risked at the stop
    "default_leverage": 10,
    "daily_loss_halt": 0.05,        # no new entries rest of UTC day after -5% of day-start equity
    "drawdown_halt": 0.40,          # no new entries for the rest of the run after -40% from peak
    "live_trading": False,
}

COSTS = {
    "taker_fee": 0.0006,            # ASSUMED 0.06% per side (BTCC schedule unverified; conservative vs typical 0.05%)
    "slippage_bps": 2.0,            # ASSUMED per side, adverse
    "half_spread_bps": 0.5,         # ASSUMED
    "funding_assumed_rate_8h": 0.0001,   # used (as a cost) ONLY where funding history is missing; flagged
    "mmr": 0.004,                   # ASSUMED maintenance margin rate (unverified)
    "min_notional": 5.0,            # ASSUMED
    "qty_step": 0.001,              # ASSUMED
    "tp_is_taker": True,
}

DATA = {
    "provider": "binance_vision",   # public archive, USDT-M perpetuals (PROXY venue; BTCC prices differ)
    "symbols": ["ETHUSDT", "BTCUSDT"],
    "base_timeframe": "1h",
    "timeframes": ["1h", "2h", "4h"],
    "start_month": "2021-01",
    "explore_frac": 0.50,
    "dev_frac": 0.80,               # remaining 20% = protected holdout
}

# Operational settings: editable from the dashboard within these bounds
SAFE_SETTINGS = {
    "workers": (1, 3, 2),                       # min, max, default
    "agent_budget_per_hour": (10, 600, 150),
    "queue_high_water": (20, 400, 100),
    "mem_pause_pct": (70, 95, 88),
    "disk_min_free_gb": (1, 100, 3),
    "job_timeout_s": (60, 7200, 900),
}


def trusted_config() -> dict:
    return {"version": CONFIG_VERSION, "goals": GOALS, "capital": CAPITAL, "thresholds": THRESHOLDS,
            "risk": RISK, "costs": COSTS, "data": DATA}


def trusted_hash() -> str:
    return hashlib.sha256(json.dumps(trusted_config(), sort_keys=True, default=str).encode()).hexdigest()


def default_settings() -> dict:
    return {k: v[2] for k, v in SAFE_SETTINGS.items()}


def validate_settings(upd: dict) -> dict:
    out = {}
    for k, v in upd.items():
        if k not in SAFE_SETTINGS:
            raise ValueError(f"unknown or non-editable setting: {k}")
        lo, hi, _ = SAFE_SETTINGS[k]
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be within [{lo},{hi}]")
        out[k] = v
    return out
