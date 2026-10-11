"""Central configuration. Persisted as JSON in the home dir; editable from the dashboard."""
from __future__ import annotations
import json, os
from dataclasses import dataclass, field, asdict
from pathlib import Path

LIVE_TRADING_ENABLED = False  # hard-coded: this code base contains no order-placement path.


def home_dir() -> Path:
    p = Path(os.environ.get("STRATEGYLAB_HOME", Path.home() / "StrategyLab"))
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    symbols: list = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    timeframes: list = field(default_factory=lambda: ["15m", "1h", "4h"])
    data_start: str = "2019-09"
    # holdout: final fraction of history never used for selection (touched once per strategy)
    train_frac: float = 0.5
    val_frac: float = 0.25          # remaining 0.25 = locked test
    # capital objective
    start_equity: float = 300.0
    horizon_months: int = 6
    withdraw_target: float = 1000.0
    retain_frac: float = 0.5
    # risk limits for qualification
    max_ruin_prob: float = 0.05
    max_drawdown: float = 0.60
    min_trades_per_week_frac: float = 0.95  # fraction of weeks with >=1 trade
    mc_paths: int = 2000
    min_target_prob: float = 0.5      # P(monthly withdrawal capacity >= $1000 within 6 months) required
    max_lose50_prob: float = 0.35
    multiple_testing_alpha: float = 0.05
    # resources
    max_rss_mb: int = 6000
    job_timeout_s: int = 600
    watchdog_stall_s: int = 900
    max_job_attempts: int = 3
    # AI
    ai_daily_budget_usd: float = 2.0
    ai_monthly_budget_usd: float = 40.0
    ai_provider_order: list = field(default_factory=lambda: ["anthropic", "openai_compat", "ollama", "deterministic"])
    ai_model_anthropic: str = "claude-sonnet-5-5"
    ai_max_retries: int = 2
    # loop pacing
    idle_sleep_s: float = 0.5
    autostart_on_boot: bool = False

    @staticmethod
    def path() -> Path:
        return home_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = cls.path()
        if p.exists():
            try:
                for k, v in json.loads(p.read_text()).items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except Exception:
                pass
        return s

    def save(self):
        self.path().write_text(json.dumps(asdict(self), indent=2))

    def update(self, d: dict):
        for k, v in d.items():
            if hasattr(self, k) and k != "autostart_on_boot":
                setattr(self, k, type(getattr(self, k))(v) if not isinstance(getattr(self, k), list) else v)
        self.save()
