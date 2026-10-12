# Data sources and exchange assumptions

## Market data
* Source: **data.binance.vision** (Binance USDT-M perpetual archive) – free, public, SHA-256 `.CHECKSUM` per file. Monthly zips; daily zips for the
  current month (lag ≈ 1 day). Klines 1h (+taker-buy volume) and monthly funding-rate history; 2h/4h are aggregated from complete 1h groups only.
* **This is a PROXY venue.** BTCC prices/basis/liquidity differ and are not modelled. No BTCC data source was reachable.
* Each dataset has a manifest row: source, retrieval time, rows, coverage (UTC), gaps, duplicates, bad rows, checksum/version, data class.
  Initial build: ETHUSDT and BTCUSDT 1h, 2021-01-01 → 2026-10-10, 50,616 bars each, 0 gaps/duplicates/bad rows, 6,297 funding events each.
* **Not available / not ingested** (and therefore never used or claimed): order book, trade-level data, liquidations, open interest, mark/index price.
  Agent 4's "order-flow" signals are **candle-level taker-buy-volume proxies**, not order-flow or institutional-flow measurements.

## BTCC specification registry (`exchange_specs`)
Every field has a status: `verified | unverified | assumed`. **Nothing BTCC-specific is verified.** A web search returned no authoritative
BTCC contract table (only a syndicated press release about fee reductions). Fees, maintenance margin tiers, funding interval, minimum sizes and the
availability of **250x/500x** are therefore `unverified`/`assumed`; research uses conservative assumptions (0.06% taker, 0.4% MMR). Goals 2–4 stay **?**.

### What the arithmetic says about 250x / 500x (independent of availability)
At 250x, initial margin = 0.40% of notional; with an assumed 0.4% maintenance margin plus 0.12% round-trip fees the liquidation buffer is **−0.12%**
(500x: −0.32%): the simulation is *invalid* – the position would be liquidated at/near entry. Real tiered MMR at such leverage would need verification.
Typical ETH 1h ATR is far larger than these buffers, so stops could not sit inside the liquidation distance.
