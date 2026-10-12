# Change log (trusted configuration / protocol)

## config 2.0.1 — post-launch calibration (controlled change)
**Found:** ~15 minutes after the first production launch (769 experiments) the live dashboard showed a hybrid candidate with ✓ on
"Positive Net Expectancy". Inspection of the database showed it had passed walk-forward at t=1.61 against a bar of only 1.20 (the bar used the
*running* count M of candidates, which was 2), and then passed the holdout under a bar (expectancy>0, PF≥1) that is close to a coin flip. A chance
result had consumed 1 of the 12 holdout looks.
**Root causes:** (1) multiple-testing bar depended on arrival order; (2) holdout test had no significance requirement; (3) the "adversarial"
randomisation test ran on the exploration region where candidates are *selected* (circular) and was not a gate; (4) goal 6 turned ✓ before the holdout.
**Changes (all via `nexus approve-config-change` after a passing test run):** `wf_expected_candidates=50` (bar ≥ 2.80); `ho_min_t=1.0`; out-of-sample
randomisation test (`adv_min_p=0.05`, 200 runs) added to the robustness gate; goal 6 requires the holdout; in-sample adversarial rows relabelled
"selection-biased, not evidence".
**Consequence handled by design:** evidence recorded under the old thresholds is automatically *stale*; stale stages are re-run, but the holdout
cannot be re-run (single look), so the early candidate can never be qualified. The old rows are preserved, not deleted.
**Other defects found by drills/live monitoring and fixed (not config):** worker heartbeat thread used a SQLite connection across threads (Guardian 1 then
restarted healthy workers every ~70 s); a superseded worker could mark its successor's process row `stopped`; supervisor `start` raced a concurrent
shutdown and its own lock probe; `config.path()` created suffix-less files as directories; random-entry streams were not prefix-stable.
