# BTCC execution model — status: UNVERIFIED ASSUMPTIONS
`api.btcc.com` was unreachable from the build environment and no BTCC specification page could be found by search, so
**no BTCC number below is verified**. They are conservative placeholders in `strategylab/btcc.py` (`verified=False`).
Verify on BTCC's official contract/fee pages (and your account's jurisdiction/product eligibility — **not checked here**)
before trusting any result, then edit `ExchangeModel`.

| Item | Assumed | Notes |
|---|---|---|
| Taker / maker fee | 0.06 % / 0.02 % per side of notional | all fills modelled as taker (conservative); fees scale with leveraged notional |
| Spread + slippage | 1 bp half-spread + 1.5 bp entry; stop-market +4 bp | gap-through stops fill at the open |
| Funding | Binance historical 8-hourly rate as proxy, settled 00/08/16 UTC | BTCC funding history not available |
| Leverage cap / MMR | ≤100× / 0.5 % | liquidation distance = 1/L − MMR (isolated); cross uses whole equity; liquidation loses the position margin |
| Min order | 0.001 BTC / 0.01 ETH, lot step same | position sized to risk %, rounded down to lot, rounded up to min lot only if margin allows |
| Margin modes | isolated + cross both simulated; sizing search picks | single position at a time, no hedge mode |
The simulation is **not** an exact reproduction of BTCC fills. Order-book/queue position, partial fills, ADL, exchange outages
and insurance-fund effects are not modelled.
