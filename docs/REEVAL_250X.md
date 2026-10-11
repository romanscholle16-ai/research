# Re-evaluation of old entries under the 250x/500x model (dev data only; 2026-10-11 20:23 UTC)

Cost model `user_500x_maker`; exits re-paired so the stop fits inside the 250x liquidation distance; viable = positive mean R in BOTH train and validation and t>2 on dev.

## dev-validated entries (old funnel passed them)
- backtested: 357
- entries: 250
- stageA_fail:frequency_single_period: 6

## entries rejected by cost/target priors before any backtest
- backtested: 480
- entries: 400
- stageA_fail:frequency_single_period: 36
- stageA_fail:insufficient_frequency: 57

Queued into the normal funnel (which runs validation + the one-shot locked test): **0**

Top dev results:

