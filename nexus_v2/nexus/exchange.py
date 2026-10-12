"""Exchange-specification registry. Every field carries a status: verified | unverified | assumed.

Nothing here has been verified against BTCC's own documentation: the BTCC site was not retrievable
from the build environment and public search returned no authoritative contract table. All BTCC
values are therefore 'assumed' (research-only, conservative) and goals 2-4 stay '?' until verified.
"""
import time
from . import config

EXCHANGE = "BTCC"


def default_specs():
    c = config.COSTS
    out = []
    for sym in ("ETHUSDT", "BTCUSDT"):
        base = [
            ("instrument_exists_btcc_perp", "unverified", "unknown", "BTCC perpetual listing for this symbol was not verified"),
            ("max_leverage_250x", "unverified", "unknown", "Availability of 250x on this contract not established from official docs"),
            ("max_leverage_500x", "unverified", "unknown", "Availability of 500x on this contract not established from official docs"),
            ("taker_fee", "assumed", c["taker_fee"], "Conservative assumption; BTCC schedule unverified"),
            ("maintenance_margin_rate", "assumed", c["mmr"], "Assumption (typical tier-1 for ETH/BTC USDT perps); unverified"),
            ("min_notional_usd", "assumed", c["min_notional"], "Assumption"),
            ("qty_step", "assumed", c["qty_step"], "Assumption"),
            ("funding_interval_hours", "assumed", 8, "Assumed 8h as on major venues; unverified for BTCC"),
            ("price_data_venue", "verified", "Binance USDT-M (data.binance.vision)",
             "Research prices come from Binance's public archive, a PROXY for BTCC; basis/spread differences are unmodelled"),
        ]
        out += [(EXCHANGE, sym, f, str(v), st, "none (not retrievable)" if st != "verified" else "data.binance.vision",
                 None if st != "verified" else time.time(), note) for f, st, v, note in base]
    return out


def seed(conn):
    for row in default_specs():
        conn.execute("INSERT OR IGNORE INTO exchange_specs(exchange,symbol,field,value,status,source,verified_at,note) "
                     "VALUES(?,?,?,?,?,?,?,?)", row)


def spec_status(conn, symbol, field):
    r = conn.execute("SELECT status,value,note FROM exchange_specs WHERE exchange=? AND symbol=? AND field=?",
                     (EXCHANGE, symbol, field)).fetchone()
    return (r["status"], r["value"], r["note"]) if r else ("unverified", None, "no record")


def leverage_feasibility(leverage: float, typical_stop_frac: float = 0.01):
    """Pure arithmetic about simulation validity at a leverage level (independent of availability)."""
    c = config.COSTS
    buffer = 1.0 / leverage - c["mmr"] - 2 * c["taker_fee"]
    rt_cost = 2 * (c["taker_fee"] + (c["slippage_bps"] + c["half_spread_bps"]) / 1e4)
    return {"leverage": leverage, "liquidation_buffer_frac": round(buffer, 6),
            "valid_simulation": buffer > 0,
            "round_trip_cost_frac": round(rt_cost, 6),
            "note": ("liquidation distance (1/L - maintenance margin - fees) is non-positive: the position would be "
                     "liquidated at/near entry under the assumed margin parameters" if buffer <= 0 else
                     f"price may move only {buffer*100:.3f}% against the position before liquidation")}
