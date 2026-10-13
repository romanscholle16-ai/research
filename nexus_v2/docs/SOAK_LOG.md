# Seven-day soak log

**Status: NO SOAK HAS STARTED. Seven-day reliability is unproven.**

| Date (UTC) | Host | Event / evidence |
|---|---|---|
| 2026-10-12 | Linux cloud VM (ephemeral, not the target) | Runtime ran for roughly 1 hour in short segments with drills; ended with the session. Not a soak. |
| 2026-10-13 | Linux cloud VM | Assignment to deploy on the user's Windows PC could not be executed: shell is not Windows. No runtime launched. |

When `START_NEXUS.bat` is run on the Windows PC, record here: start time (UTC), `CHECK_NEXUS.bat` output, then daily `CHECK_NEXUS.bat`,
`python -m nexus status`, restarts per process, incidents (open/recurring), disk/RAM trend; finish with `python -m nexus integrity` and the counters.
