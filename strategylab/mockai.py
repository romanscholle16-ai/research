"""Scripted AI provider for acceptance tests ONLY (simulates valid output, garbage output and quota exhaustion)."""
import json
from .ai import Provider, QuotaExhausted


class MockProvider(Provider):
    name = "mock"
    def __init__(self, db): self.db = db
    def available(self): return True
    def complete(self, system, prompt, max_tokens):
        n = self.db.get("mock_calls", 0) + 1; self.db.set("mock_calls", n)
        if n % 5 == 0: raise QuotaExhausted("simulated quota exhausted")
        if n % 3 == 0: return "not json at all", 10, 5
        prop = [{"family": "tsmom", "params": {"lookback": 24, "thr_atr": 0.5}, "side": "both", "stop_atr": 2.0, "target_rr": 2.0, "max_hold": 48,
                 "filters": [], "timeframe": "1h", "symbol": "BTCUSDT", "hypothesis": "mock", "falsification": "mock"},
                {"family": "nonexistent", "params": {}, "side": "both", "stop_atr": 2.0, "target_rr": 2.0, "max_hold": 48, "timeframe": "1h", "symbol": "BTCUSDT"}]
        prop[0]["params"]["lookback"] = [12, 24, 48, 96][n % 4]
        return json.dumps(prop), 200, 100
