# ETH opportunity research (2026-10-11 21:18 UTC)

## 1. ETH data completeness

- 1h: ok, rows=59400, missing=0.0, longest gap=1.0h, checksum=6a08b0aa53456bbc
- 15m: ok, rows=237600, missing=0.0, longest gap=0.25h, checksum=0be9b30a7acb3bed
- 5m: ok, rows=712800, missing=0.0, longest gap=0.08h, checksum=e5b2eb1925d4d17a
- 1m raw archive (2020-01-01 .. 2026-10-10): 3,564,000 rows, 0 missing minutes (checked directly). One junk 'numeric header' row per daily-sync file (open_time=0) was found and is now dropped on read; 5m+ research frames were never affected.

## 2. Leverage arithmetic (identical regardless of strategy)

| leverage | liquidation distance | max safe stop (80%) | move for +400% on margin | round-trip fee as % of margin (maker/maker) | (maker entry + taker stop) |
|---|---|---|---|---|---|
| 25x | 3.90% | 3.12% | 16.00% | 2% | 3% |
| 50x | 1.90% | 1.52% | 8.00% | 3% | 7% |
| 100x | 0.90% | 0.72% | 4.00% | 6% | 14% |
| 250x | 0.30% | 0.24% | 1.60% | 15% | 35% |
| 500x | 0.10% | 0.08% | 0.80% | 30% | 70% |

Assumptions (UNVERIFIED for BTCC): maintenance margin 0.1%, maker 0.03%, taker 0.06%, half-spread 0.01%, stop slippage 0.04%. At 500x the liquidation distance (0.10%) is below the 0.10% minimum stop the backtester accepts, i.e. no stop fits inside liquidation; it is excluded by arithmetic under these assumptions.

## 3. Opportunity census (train period, ETHUSDT, passive limit entries, stop sized for each leverage)

| tf | lev | stop | target | side | tradable bars | random-entry win rate | zero-edge win rate | median/p90 adverse excursion before a win | wins as share of bars |
|---|---|---|---|---|---|---|---|---|---|
| 15m | 250x | 0.2% | 0.4% | long | 56% | 24.9% | 33.3% | 0.079% / 0.161% | 13.9% |
| 15m | 250x | 0.2% | 0.6% | long | 56% | 20.2% | 25.0% | 0.079% / 0.161% | 11.3% |
| 15m | 250x | 0.2% | 0.8% | long | 56% | 16.7% | 20.0% | 0.079% / 0.161% | 9.3% |
| 15m | 250x | 0.2% | 0.4% | short | 56% | 25.1% | 33.3% | 0.079% / 0.162% | 14.1% |
| 15m | 250x | 0.2% | 0.6% | short | 56% | 20.6% | 25.0% | 0.078% / 0.163% | 11.5% |
| 15m | 250x | 0.2% | 0.8% | short | 56% | 16.9% | 20.0% | 0.079% / 0.163% | 9.5% |
| 15m | 100x | 0.5% | 1.0% | long | 76% | 28.8% | 33.3% | 0.169% / 0.405% | 21.9% |
| 15m | 100x | 0.5% | 1.5% | long | 76% | 18.6% | 25.0% | 0.163% / 0.396% | 14.1% |
| 15m | 100x | 0.5% | 2.0% | long | 76% | 12.1% | 20.0% | 0.157% / 0.393% | 9.2% |
| 15m | 100x | 0.5% | 1.0% | short | 76% | 29.4% | 33.3% | 0.168% / 0.402% | 22.4% |
| 15m | 100x | 0.5% | 1.5% | short | 76% | 19.1% | 25.0% | 0.164% / 0.396% | 14.6% |
| 15m | 100x | 0.5% | 2.0% | short | 76% | 12.8% | 20.0% | 0.16% / 0.39% | 9.7% |
| 15m | 50x | 1.0% | 2.0% | long | 79% | 16.1% | 33.3% | 0.227% / 0.642% | 12.7% |
| 15m | 50x | 1.0% | 3.0% | long | 79% | 7.6% | 25.0% | 0.211% / 0.61% | 6.0% |
| 15m | 50x | 1.0% | 4.0% | long | 79% | 3.9% | 20.0% | 0.193% / 0.578% | 3.1% |
| 15m | 50x | 1.0% | 2.0% | short | 79% | 16.9% | 33.3% | 0.224% / 0.646% | 13.4% |
| 15m | 50x | 1.0% | 3.0% | short | 79% | 7.8% | 25.0% | 0.207% / 0.6% | 6.2% |
| 15m | 50x | 1.0% | 4.0% | short | 79% | 3.8% | 20.0% | 0.198% / 0.563% | 3.0% |
| 15m | 25x | 2.0% | 4.0% | long | 80% | 4.2% | 33.3% | 0.218% / 0.737% | 3.4% |
| 15m | 25x | 2.0% | 6.0% | long | 80% | 1.2% | 25.0% | 0.176% / 0.614% | 1.0% |
| 15m | 25x | 2.0% | 8.0% | long | 80% | 0.4% | 20.0% | 0.188% / 0.656% | 0.3% |
| 15m | 25x | 2.0% | 4.0% | short | 81% | 4.1% | 33.3% | 0.224% / 0.755% | 3.3% |
| 15m | 25x | 2.0% | 6.0% | short | 81% | 1.2% | 25.0% | 0.201% / 0.698% | 1.0% |
| 15m | 25x | 2.0% | 8.0% | short | 81% | 0.5% | 20.0% | 0.188% / 0.571% | 0.4% |
| 5m | 250x | 0.2% | 0.4% | long | 51% | 28.9% | 33.3% | 0.073% / 0.156% | 14.8% |
| 5m | 250x | 0.2% | 0.6% | long | 51% | 22.3% | 25.0% | 0.075% / 0.157% | 11.4% |
| 5m | 250x | 0.2% | 0.8% | long | 51% | 17.8% | 20.0% | 0.076% / 0.158% | 9.1% |
| 5m | 250x | 0.2% | 0.4% | short | 52% | 29.5% | 33.3% | 0.073% / 0.156% | 15.2% |
| 5m | 250x | 0.2% | 0.6% | short | 52% | 22.6% | 25.0% | 0.076% / 0.158% | 11.7% |
| 5m | 250x | 0.2% | 0.8% | short | 52% | 18.0% | 20.0% | 0.077% / 0.159% | 9.3% |
| 5m | 100x | 0.5% | 1.0% | long | 72% | 27.6% | 33.3% | 0.15% / 0.384% | 20.0% |
| 5m | 100x | 0.5% | 1.5% | long | 72% | 16.6% | 25.0% | 0.146% / 0.373% | 12.0% |
| 5m | 100x | 0.5% | 2.0% | long | 72% | 10.3% | 20.0% | 0.137% / 0.362% | 7.4% |
| 5m | 100x | 0.5% | 1.0% | short | 73% | 28.2% | 33.3% | 0.149% / 0.378% | 20.5% |
| 5m | 100x | 0.5% | 1.5% | short | 73% | 17.3% | 25.0% | 0.143% / 0.367% | 12.5% |
| 5m | 100x | 0.5% | 2.0% | short | 73% | 11.0% | 20.0% | 0.139% / 0.36% | 8.0% |
| 5m | 50x | 1.0% | 2.0% | long | 76% | 13.0% | 33.3% | 0.188% / 0.573% | 9.8% |
| 5m | 50x | 1.0% | 3.0% | long | 76% | 5.6% | 25.0% | 0.173% / 0.544% | 4.2% |
| 5m | 50x | 1.0% | 4.0% | long | 76% | 2.7% | 20.0% | 0.159% / 0.523% | 2.1% |
| 5m | 50x | 1.0% | 2.0% | short | 76% | 13.8% | 33.3% | 0.189% / 0.568% | 10.4% |
| 5m | 50x | 1.0% | 3.0% | short | 76% | 5.9% | 25.0% | 0.175% / 0.526% | 4.5% |
| 5m | 50x | 1.0% | 4.0% | short | 76% | 2.7% | 20.0% | 0.163% / 0.503% | 2.1% |
| 5m | 25x | 2.0% | 4.0% | long | 77% | 2.9% | 33.3% | 0.176% / 0.636% | 2.2% |
| 5m | 25x | 2.0% | 6.0% | long | 77% | 0.8% | 25.0% | 0.14% / 0.518% | 0.6% |
| 5m | 25x | 2.0% | 8.0% | long | 77% | 0.3% | 20.0% | 0.151% / 0.545% | 0.2% |
| 5m | 25x | 2.0% | 4.0% | short | 77% | 2.9% | 33.3% | 0.183% / 0.637% | 2.2% |
| 5m | 25x | 2.0% | 6.0% | short | 77% | 0.8% | 25.0% | 0.172% / 0.56% | 0.6% |
| 5m | 25x | 2.0% | 8.0% | short | 77% | 0.3% | 20.0% | 0.161% / 0.504% | 0.3% |

'Zero-edge win rate' = 1/(1+R:R), the hit rate needed to break even with zero costs; random entries fall below it because passive entries fill mostly when price is already moving against the position (adverse selection).

## 4. Pattern discovery (train-only) and prediction (embargoed validation)

- finalists persisted: 576; validated (val mean R>0, t>=1.28, n>=40): **0**
| tf | lev | side | R:R | train best (mean R / t / n) | validation (mean R / t / n / win) | status |
|---|---|---|---|---|---|---|
| 15m | 25x | long | 2.0 | +0.242 / 3.99 / 220 | -0.018 / -0.22 / 127 / 8.7% | rejected_on_validation |
| 15m | 25x | long | 3.0 | +0.219 / 3.60 / 220 | -0.014 / -0.17 / 127 / 2.4% | rejected_on_validation |
| 15m | 25x | long | 4.0 | +0.217 / 3.56 / 220 | -0.012 / -0.15 / 127 / 0.8% | rejected_on_validation |
| 15m | 25x | short | 2.0 | +0.089 / 2.03 / 348 | -0.043 / -0.91 / 305 / 9.5% | rejected_on_validation |
| 15m | 25x | short | 3.0 | +0.039 / 0.73 / 293 | -0.098 / -2.19 / 378 / 3.2% | rejected_on_validation |
| 15m | 25x | short | 4.0 | +0.031 / 0.59 / 293 | -0.092 / -1.97 / 378 / 1.9% | rejected_on_validation |
| 15m | 50x | long | 2.0 | +0.230 / 3.40 / 359 | -0.247 / -2.93 / 224 / 21.0% | rejected_on_validation |
| 15m | 50x | long | 3.0 | +0.292 / 3.73 / 359 | -0.245 / -2.64 / 224 / 10.7% | rejected_on_validation |
| 15m | 50x | long | 4.0 | +0.320 / 3.79 / 359 | -0.265 / -2.82 / 224 / 4.9% | rejected_on_validation |
| 15m | 50x | short | 2.0 | +0.089 / 1.44 / 447 | -0.093 / -0.99 / 187 / 25.1% | rejected_on_validation |
| 15m | 50x | short | 3.0 | +0.049 / 0.89 / 450 | -0.322 / -3.95 / 163 / 2.5% | rejected_on_validation |
| 15m | 50x | short | 4.0 | +0.130 / 1.41 / 286 | -0.169 / -1.73 / 252 / 8.7% | rejected_on_validation |
| 15m | 100x | long | 2.0 | +0.150 / 1.63 / 285 | -0.237 / -1.94 / 156 / 34.6% | rejected_on_validation |
| 15m | 100x | long | 3.0 | +0.233 / 2.07 / 285 | -0.220 / -1.54 / 156 / 23.1% | rejected_on_validation |
| 15m | 100x | long | 4.0 | +0.316 / 2.44 / 285 | -0.183 / -1.15 / 156 / 16.7% | rejected_on_validation |
| 15m | 100x | short | 2.0 | -0.003 / -0.03 / 160 | -0.480 / -3.92 / 148 / 27.7% | rejected_on_validation |
| 15m | 100x | short | 3.0 | +0.128 / 0.82 / 146 | -0.633 / -4.35 / 123 / 17.1% | rejected_on_validation |
| 15m | 100x | short | 4.0 | +0.190 / 0.94 / 120 | -0.386 / -2.13 / 126 / 18.3% | rejected_on_validation |
| 15m | 250x | long | 2.0 | -0.607 / -4.94 / 171 | -0.751 / -6.48 / 191 / 30.4% | rejected_on_validation |
| 15m | 250x | long | 3.0 | -0.383 / -2.40 / 173 | -0.951 / -4.73 / 91 / 22.0% | rejected_on_validation |
| 15m | 250x | long | 4.0 | -0.299 / -1.72 / 188 | -0.807 / -5.21 / 193 / 18.7% | rejected_on_validation |
| 15m | 250x | short | 2.0 | -0.508 / -3.32 / 124 | -0.842 / -4.25 / 74 / 32.4% | rejected_on_validation |
| 15m | 250x | short | 3.0 | -0.415 / -2.21 / 124 | -1.010 / -4.54 / 74 / 21.6% | rejected_on_validation |
| 15m | 250x | short | 4.0 | -0.232 / -1.04 / 124 | -0.943 / -3.67 / 74 / 18.9% | rejected_on_validation |
| 5m | 25x | long | 2.0 | +0.112 / 2.92 / 327 | -0.097 / -1.91 / 182 / 2.2% | rejected_on_validation |
| 5m | 25x | long | 3.0 | +0.115 / 2.92 / 327 | -0.093 / -1.80 / 182 / 0.5% | rejected_on_validation |
| 5m | 25x | long | 4.0 | +0.119 / 2.93 / 327 | -0.088 / -1.63 / 182 / 0.5% | rejected_on_validation |
| 5m | 25x | short | 2.0 | +0.007 / 0.33 / 998 | -0.043 / -1.56 / 701 / 5.4% | rejected_on_validation |
| 5m | 25x | short | 3.0 | -0.003 / -0.14 / 777 | -0.024 / -0.78 / 604 / 2.6% | rejected_on_validation |
| 5m | 25x | short | 4.0 | -0.004 / -0.13 / 682 | -0.151 / -4.15 / 475 / 1.1% | rejected_on_validation |
| 5m | 50x | long | 2.0 | +0.128 / 2.89 / 724 | -0.226 / -4.10 / 487 / 18.7% | rejected_on_validation |
| 5m | 50x | long | 3.0 | +0.136 / 2.79 / 724 | -0.233 / -3.91 / 487 / 8.8% | rejected_on_validation |
| 5m | 50x | long | 4.0 | +0.151 / 2.94 / 724 | -0.242 / -3.99 / 487 / 3.7% | rejected_on_validation |
| 5m | 50x | short | 2.0 | +0.027 / 0.54 / 472 | -0.056 / -1.08 / 436 / 15.6% | rejected_on_validation |
| 5m | 50x | short | 3.0 | +0.055 / 0.97 / 472 | -0.048 / -0.86 / 436 / 7.6% | rejected_on_validation |
| 5m | 50x | short | 4.0 | +0.059 / 0.95 / 404 | -0.075 / -1.22 / 428 / 4.2% | rejected_on_validation |
| 5m | 100x | long | 2.0 | +0.111 / 1.58 / 445 | -0.197 / -2.44 / 343 / 32.9% | rejected_on_validation |
| 5m | 100x | long | 3.0 | +0.170 / 2.00 / 465 | -0.330 / -3.37 / 315 / 21.3% | rejected_on_validation |
| 5m | 100x | long | 4.0 | +0.143 / 1.62 / 548 | -0.406 / -4.12 / 362 / 13.8% | rejected_on_validation |
| 5m | 100x | short | 2.0 | -0.039 / -0.68 / 618 | -0.180 / -2.94 / 555 / 30.6% | rejected_on_validation |
| 5m | 100x | short | 3.0 | +0.013 / 0.17 / 557 | -0.245 / -3.48 / 570 / 19.6% | rejected_on_validation |
| 5m | 100x | short | 4.0 | +0.035 / 0.39 / 440 | -0.123 / -1.41 / 441 / 12.9% | rejected_on_validation |
| 5m | 250x | long | 2.0 | -0.411 / -4.62 / 373 | -0.807 / -6.81 / 199 / 31.7% | rejected_on_validation |
| 5m | 250x | long | 3.0 | -0.474 / -2.87 / 158 | -1.233 / -6.26 / 77 / 16.9% | rejected_on_validation |
| 5m | 250x | long | 4.0 | -0.487 / -2.83 / 189 | -1.171 / -5.40 / 86 / 15.1% | rejected_on_validation |
| 5m | 250x | short | 2.0 | -0.481 / -5.60 / 364 | -0.714 / -8.06 / 336 / 31.5% | rejected_on_validation |
| 5m | 250x | short | 3.0 | -0.427 / -3.85 / 350 | -0.741 / -4.72 / 167 / 26.9% | rejected_on_validation |
| 5m | 250x | short | 4.0 | -0.421 / -3.22 / 317 | -0.811 / -6.05 / 253 / 18.2% | rejected_on_validation |

## 5. Cost ladder: gross predictability ceiling (discovery repeated at zero cost, validated at several cost levels)

| tf | lev | side | R:R | val mean R at cost x0 | x0.25 | x0.5 | x1.0 | best-pattern conditions |
|---|---|---|---|---|---|---|---|---|
| 15m | 250x | long | 3.0 | -0.049 (t -0.4, n 218) | -0.268 (t -2.2, n 218) | -0.487 (t -3.9, n 218) | -0.925 (t -6.9, n 218) | vz<=-0.85 & mom24<=-1.06 & brk_dn>-2.23 |
| 15m | 250x | short | 3.0 | +0.014 (t 0.1, n 340) | -0.198 (t -2.0, n 340) | -0.409 (t -4.0, n 340) | -0.832 (t -7.7, n 340) | hour4<=0.0 & wick_lo>0.47 & mom6>-0.58 |
| 15m | 100x | long | 3.0 | -0.010 (t -0.1, n 277) | -0.105 (t -1.0, n 277) | -0.200 (t -1.9, n 277) | -0.390 (t -3.6, n 277) | mom24>3.83 & rng_exp>1.55 & atr_ratio>1.12 |
| 15m | 100x | short | 3.0 | -0.057 (t -0.9, n 559) | -0.167 (t -2.5, n 559) | -0.276 (t -4.1, n 559) | -0.495 (t -7.1, n 559) | hour4<=0.0 & wick_lo>0.47 |
| 15m | 50x | long | 3.0 | -0.053 (t -0.6, n 224) | -0.101 (t -1.1, n 224) | -0.149 (t -1.6, n 224) | -0.245 (t -2.6, n 224) | mom24>3.85 & atr_ratio>1.27 & imbz>0.55 |
| 15m | 50x | short | 3.0 | -0.005 (t -0.1, n 393) | -0.063 (t -1.1, n 393) | -0.121 (t -2.0, n 393) | -0.236 (t -3.9, n 393) | hour4<=0.0 & vwap<=-0.66 |
| 5m | 250x | long | 3.0 | -0.063 (t -0.5, n 151) | -0.278 (t -1.9, n 151) | -0.494 (t -3.3, n 151) | -0.926 (t -5.8, n 151) | ema200>5.95 & ema50<=1.12 & brk_dn>-1.47 |
| 5m | 250x | short | 3.0 | -0.076 (t -0.8, n 340) | -0.271 (t -2.9, n 340) | -0.466 (t -4.8, n 340) | -0.855 (t -8.2, n 340) | ema200<=-4.19 & mom3>0.01 & brk_up>-2.77 |
| 5m | 100x | long | 3.0 | +0.007 (t 0.1, n 487) | -0.081 (t -1.1, n 487) | -0.168 (t -2.2, n 487) | -0.343 (t -4.4, n 487) | atr_ratio>1.26 & ema200>4.03 & btc_lead<=-0.11 |
| 5m | 100x | short | 3.0 | +0.045 (t 0.9, n 935) | -0.034 (t -0.6, n 935) | -0.112 (t -2.1, n 935) | -0.270 (t -5.0, n 935) | vwap<=0.3 & atr_pct>0.84 |
| 5m | 50x | long | 3.0 | -0.067 (t -1.3, n 579) | -0.119 (t -2.3, n 579) | -0.171 (t -3.2, n 579) | -0.275 (t -5.1, n 579) | er20>0.4 & ema200>4.04 & atr_ratio>1.11 |
| 5m | 50x | short | 3.0 | -0.036 (t -0.9, n 929) | -0.094 (t -2.3, n 929) | -0.153 (t -3.7, n 929) | -0.270 (t -6.4, n 929) | ema200<=-2.72 & swp_lo>0.0 & btc_lead>0.0 |
