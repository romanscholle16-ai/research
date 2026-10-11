"""BTCC USDT-margined perpetual execution assumptions.

IMPORTANT - VERIFICATION STATUS: BTCC's live fee schedule, leverage tiers, maintenance-margin tiers and
minimum order sizes could NOT be verified from this environment (api.btcc.com unreachable; web search
returned no BTCC specification). Every number below is a CONSERVATIVE ASSUMPTION modelled on typical
Tier-1 USDT-perp venues (taker 5-6 bps, maker 2 bps, MMR 0.4-0.5% at small size) and flagged `verified=False`.
Replace via settings once confirmed from BTCC's official contract page. The simulation must never be
described as an exact reproduction of BTCC execution.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ExchangeModel:
    name: str = "BTCC (assumed, unverified)"
    verified: bool = False
    taker_fee: float = 0.0006       # per side, fraction of notional (conservative)
    maker_fee: float = 0.0002
    half_spread: float = 0.00010    # fraction; applied on market entries/exits
    slippage: float = 0.00015       # extra fraction on market fills (entry)
    stop_slippage: float = 0.00040  # extra slippage on stop-market fills
    max_leverage: int = 100
    maint_margin_rate: float = 0.005  # small-size tier assumption
    liq_fee: float = 0.005          # extra fee charged by exchange at liquidation (fraction of notional)
    min_qty: dict = None            # contract min quantity (base units)
    funding_interval_h: int = 8
    cost_mult: float = 1.0          # for cost-sensitivity analysis

    def minq(self, symbol):
        return (self.min_qty or {"BTCUSDT": 0.001, "ETHUSDT": 0.01}).get(symbol, 0.01)

    def entry_cost(self):   # fraction of notional, market-in
        return (self.taker_fee + self.half_spread + self.slippage) * self.cost_mult

    def exit_cost(self, stop=False):
        return (self.taker_fee + self.half_spread + (self.stop_slippage if stop else self.slippage)) * self.cost_mult

    def liq_distance(self, leverage):
        """Adverse fractional move from entry that triggers liquidation (isolated margin, no extra margin added)."""
        return max(1.0 / leverage - self.maint_margin_rate, 0.0)

    def with_cost_mult(self, m):
        from dataclasses import replace
        return replace(self, cost_mult=m)


DEFAULT = ExchangeModel()
