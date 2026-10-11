# BTCC execution model: verified vs unverified (checked 2026-10-11)
Sources: btcc.com academy fee guide (page says "Last updated 2026-06-03"), btcc.com BTCUSDT trade page, btcc.com USDT-M overview (marketing only). BTCC offers no
API or bulk history reachable from the build environment, and its own pages **contradict each other**, so nothing below is treated as final. Re-check on the live fee
page and your account before relying on any number. Eligibility/jurisdiction for your account was **not** checked.

| Item | Finding | Status | Model default |
|---|---|---|---|
| Taker fee (USDT-M, VIP0) | 0.048% (fee guide) / 0.045% (trade page) / up to 0.06% (guide FAQ) | CONFLICTING | 0.06% (conservative end); preset `listed_vip0` uses 0.048% |
| Maker fee | 0.02% (trade page) / 0.03% (fee guide) | CONFLICTING | unused, every fill modelled as taker |
| Funding | exchanged between longs and shorts every 8h at 00/08/16 UTC | stated by BTCC | same schedule; **Binance historical rates as proxy** |
| BTCUSDT contract size | 0.001 BTC | stated | 0.001 BTC min lot |
| ETHUSDT minimum | not found | UNVERIFIED | 0.01 ETH |
| Max leverage | BTCUSDT up to 125x on one page; tiers not found | PARTIAL | 100x cap; sizing search <=50x |
| Maintenance margin / liquidation rules / liquidation fee | not found | UNVERIFIED | 0.5% / isolated+cross formulas / 0.5% |
| Spread, slippage | no BTCC quote history | UNAVAILABLE | 1 bp + 1.5 bp entry, +4 bp stop-market; stressed 1.5x/2x |
| Price history | Binance USD-M archive used as proxy | LIMITATION | BTCC prices can differ (basis, wicks, outages) |

Select a preset with the `exchange_preset` setting (`conservative`, `listed_vip0`, `adverse`); provenance strings live in `strategylab/btcc.py::PROVENANCE`.
Backtests on Binance data do **not** prove equivalent BTCC performance. Order-book queueing, partial fills, ADL and outages are not modelled.
