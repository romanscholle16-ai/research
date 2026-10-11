# Known limitations (kept honest)
* **Data**: Binance USD-M public archive (klines incl. taker-buy volume, funding) is a *proxy* for BTCC. Open interest, liquidations,
  order-book, bid/ask history are **not loaded**; hypotheses needing them are recorded as `blocked_data` and never faked. Taker-buy volume is a CVD *proxy*.
  Archive 1-minute data starts Sep 2019 (BTC) / Nov 2019 (ETH). Gaps are counted, not filled.
* **Bar-level simulation**: intrabar path unknown → stop-first rule in ambiguous bars; no tick replay; no partial fills.
* **Capital Monte Carlo** resamples historical monthly trade blocks: it assumes the future resembles the sampled past and is *not* a forecast.
  Short, regime-specific history inflates apparent edge; the locked-test + multiple-testing gates exist to counter that, not eliminate it.
* **Multiple testing** uses Šidák over all strategies tested (conservative). It makes qualification very hard by design as the trial count grows.
* **Session-drift hours** are fitted on the train slice (a selection step) — screened in-sample, then judged out-of-sample.
* $300 → $1,000+/month in 6 months implies ~+700 %+ monthly-profit capacity at 50 % retention; honest outcome may well be "no strategy qualifies".
* AI-proposed ideas are only as good as the pipeline that tests them; AI cannot change results.
* The acceptance test uses synthetic data: it proves engine behaviour, not alpha, and not 24/7 stability.
