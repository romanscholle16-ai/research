# Outlook — what this data can realistically support (generated 2026-10-11 18:57 UTC)

## 1. Return ladder from locked-test finalists (last 3 days, non-4h) -- plain arithmetic: trades/month x edge per trade x risk per trade

- 1906 finalists with >=20 locked-test trades; edge per trade > 0 in 46%; statistically significant (t>2) in 1.
- Monthly return if risking 2% of equity per trade: median -0.11%, best 10.0%; at 5% risk: median -0.28%, best 24.9% (single best of ~1906, i.e. selection-inflated).
- The target needs about +61% per month (compounded) to reach $1,000/month withdrawable within 6 months. Nothing in the data comes within a factor of ~10 of that without leverage that makes ruin likely.
- (The $300 capital simulation in the final stage is not used here: its wealth outputs could not be reconciled with these per-trade numbers and need an audit.)

## 2. Funding-rate income ceiling (hold the paying side of perpetual funding; ignores price risk and fees)

| market | mean funding / 8h | annualised | best 90d window (annualised) | worst 90d window (annualised) | share of 8h periods paying shorts |
|---|---|---|---|---|---|
| BTCUSDT | 0.0107% | 11.7% | 68% | -2% | 86% |
| ETHUSDT | 0.0126% | 13.8% | 87% | -7% | 86% |
| SOLUSDT | 0.0002% | 0.2% | 80% | -173% | 71% |
| XRPUSDT | 0.0133% | 14.6% | 112% | -5% | 80% |
| BNBUSDT | -0.0000% | -0.0% | 81% | -25% | 26% |

Even the best 90-day windows are a small fraction of +61% per month, and the income is only collected while accepting full price risk (or a hedge that doubles costs and needs a spot venue BTCC may not offer).
