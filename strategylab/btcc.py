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
    high_leverage: bool = False     # user-directed mode: strategies must be sizeable at min_lev_capable..max_leverage (250x/500x) with liquidation modelled
    min_lev_capable: int = 0
    limit_bars: int = 0             # >0: entries are passive limit orders at the signal-bar close, live for this many bars (maker fee, no spread/slippage; fills only if price trades through)
    limit_exits: bool = False       # targets are resting limit orders (maker fee; fill only if price trades through); stops/time exits stay taker
    fill_pen: float = 0.00005       # price must trade this fraction THROUGH a limit before it counts as filled (queue-position haircut)

    def minq(self, symbol):
        return (self.min_qty or {"BTCUSDT": 0.001, "ETHUSDT": 0.01}).get(symbol, 0.01)

    def entry_cost(self):   # fraction of notional
        if self.limit_bars: return self.maker_fee * self.cost_mult
        return (self.taker_fee + self.half_spread + self.slippage) * self.cost_mult

    def exit_cost(self, stop=False):
        if not stop and self.limit_exits: return self.maker_fee * self.cost_mult
        return (self.taker_fee + self.half_spread + (self.stop_slippage if stop else self.slippage)) * self.cost_mult

    def round_trip(self):
        return self.entry_cost() + 0.5 * self.exit_cost(stop=True) + 0.5 * self.exit_cost(stop=False)

    def tradable_stop(self, sfrac, max_cost_R=1.5):
        """High-leverage execution rule (known at signal time): the stop must sit inside the liquidation distance at min_lev_capable and
        the round trip must cost <= max_cost_R of the 1R risk, otherwise the trade is skipped. No-op for normal presets."""
        import numpy as np
        sfrac = np.asarray(sfrac, float)
        if not self.min_lev_capable: return np.ones(sfrac.shape, bool)
        with np.errstate(divide="ignore", invalid="ignore"):
            return (sfrac <= 0.8 * self.liq_distance(self.min_lev_capable)) & (self.round_trip() / sfrac <= max_cost_R)

    def liq_distance(self, leverage):
        """Adverse fractional move from entry that triggers liquidation (isolated margin, no extra margin added)."""
        return max(1.0 / leverage - self.maint_margin_rate, 0.0)

    def with_cost_mult(self, m):
        from dataclasses import replace
        return replace(self, cost_mult=m)


# Provenance of every parameter (checked 2026-10-11 against btcc.com pages; BTCC publishes no API/bulk data reachable from here).
PROVENANCE = {
    "taker_fee": "CONFLICTING on btcc.com: 0.048% VIP0 (academy fee guide, updated 2026-06-03), 0.045% (BTCUSDT trade page), up to 0.06% (same guide FAQ). Default uses 0.06% = conservative end.",
    "maker_fee": "CONFLICTING: 0.02% (trade page) vs 0.03% (VIP0 fee guide). Not used by default (all fills modelled taker).",
    "funding": "VERIFIED-ish (BTCC pages): paid between longs/shorts every 8h at 00:00/08:00/16:00 UTC. HISTORICAL BTCC rates unavailable: Binance rates used as proxy.",
    "contract_size_min_qty": "BTCUSDT contract size 0.001 BTC (BTCC trade page). ETHUSDT minimum NOT found: 0.01 ETH assumed.",
    "max_leverage": "BTCUSDT up to 125x listed on a BTCC page; per-tier limits NOT found. 100x cap assumed; sizing search only uses <=50x.",
    "maint_margin_rate": "NOT FOUND. 0.5% small-size assumption. Liquidation fee NOT FOUND (0.5% assumed).",
    "spread_slippage": "NOT AVAILABLE (no BTCC quote history). Conservative assumption; stressed via cost multipliers.",
    "eligibility": "NOT CHECKED: product availability and jurisdiction restrictions must be confirmed by the account holder before any live use.",
}

PRESETS = {
    "conservative": ExchangeModel(),                                                   # default: taker 0.06%
    "listed_vip0": ExchangeModel(name="BTCC (listed VIP0 fee guide)", taker_fee=0.00048, maker_fee=0.0003),
    "adverse": ExchangeModel(name="BTCC (adverse)", taker_fee=0.0006, half_spread=0.0002, slippage=0.0004, stop_slippage=0.0008),
    # USER-ASSERTED 500x availability (not verified by us): maintenance margin 0.1% and liquidation fee 0.1% are ASSUMPTIONS; the stop must fit inside the liquidation distance at 250x.
    "user_500x": ExchangeModel(name="BTCC 250-500x (user-asserted, unverified; conservative fees)", max_leverage=500, maint_margin_rate=0.001, liq_fee=0.001, high_leverage=True, min_lev_capable=250),
    "user_500x_listed": ExchangeModel(name="BTCC 250-500x (user-asserted; listed VIP0 taker 0.048%, tighter slippage)", taker_fee=0.00048, maker_fee=0.0003, half_spread=0.00005,
                                      slippage=0.0001, stop_slippage=0.0003, max_leverage=500, maint_margin_rate=0.001, liq_fee=0.001, high_leverage=True, min_lev_capable=250),
    # Passive-execution variant: limit entries/targets (maker fee, fill only when price trades through), taker stops. Fees: VIP0 maker 0.03% (conservative end of the conflicting 0.02-0.03%), taker 0.06%.
    "user_500x_maker": ExchangeModel(name="BTCC 250-500x (user-asserted; limit entries/targets, taker stops)", taker_fee=0.0006, maker_fee=0.0003, max_leverage=500, maint_margin_rate=0.001,
                                     liq_fee=0.001, high_leverage=True, min_lev_capable=250, limit_bars=3, limit_exits=True),
}


def get_model(name="conservative"):
    return PRESETS.get(name, PRESETS["conservative"])


DEFAULT = ExchangeModel()
