# Unattended readiness (PASS / FAIL / BLOCKED / NOT TESTED)

Written from a Linux cloud VM, **not** the target Windows PC. Every Windows item is therefore BLOCKED or NOT TESTED.

| Requirement | Status | Evidence |
|---|---|---|
| Execution environment is the user's local Windows PC | **FAIL** | Linux cloud VM (uname Linux 6.18 x86_64, not WSL, CLAUDE_CODE_REMOTE=true, no cmd.exe/schtasks.exe). Nothing was installed on the user's PC. |
| Existing implementation found and preserved | **PASS** | branch claude/confident-gauss-l66zf7 commit 38c603b present; var/ DB, datasets and history intact on this VM; 92 prior tests still pass inside the 112 |
| Windows-compat code (CPU/RSS via ctypes, priority, per-process logs, UTF-8, atomic replace) | **PASS** | implemented + 16 OS-independent tests; NOT executed on Windows |
| START/STOP/RESTART/CHECK/INSTALL_AUTOSTART/UNINSTALL_AUTOSTART .bat created | **PASS** | static checks only: CRLF, ASCII, required steps present, no system-wide changes |
| Batch files executed on Windows | **NOT TESTED** | no Windows host available |
| Python launcher logic (preflight, duplicate refusal, real health verification) | **PASS** | tests/test_runtime_drills.py::test_launch_command_* on Linux with real processes |
| venv creation/repair and dependency install on Windows | **NOT TESTED** | logic in START_NEXUS.bat only |
| Windows process creation/termination, msvcrt supervisor lock | **NOT TESTED** | 3 Windows-only tests skipped |
| Auto-start Scheduled Task configured and verified | **BLOCKED** | needs a Windows host; XML task generation and its fields are unit-tested (no time limit, battery-safe, per-user, no elevation) |
| unattended_7_day profile defined and bounds-checked | **PASS** | config.PROFILES; applied by `nexus launch` |
| Profile tuned on the GTX 1070 / 16 GB machine | **NOT TESTED** | only Linux 4-core/16 GB measurements exist (3-5% CPU, ~110 MB per worker at 150 exp/agent/h) |
| GPU detection on GTX 1070 | **NOT TESTED** | nvidia-smi parsing implemented; no GPU on this VM; GPU not used for research |
| Production research launch on Windows | **BLOCKED** | requires running START_NEXUS.bat on the PC |
| Runtime survives launcher/terminal exit | **PASS (Linux only)** | supervisor PPID 1 own session; drills; NOT TESTED on Windows (DETACHED_PROCESS path) |
| Fault-injection drills (worker/supervisor kill, dup start, poison job, mem/disk, DB corruption, AI outage, stalled job) | **PASS (Linux only)** | 13 drills + launcher drill; Windows variants use taskkill but were not run |
| Trusted backtester hand-calculated references | **PASS** | 18 engine tests |
| Validation safeguards (multiple-testing floor, holdout significance, OOS randomisation, version-keyed revalidation) | **PASS** | verified in code and 21 validation tests |
| Ten agents / three guardians distinct and independent | **PASS** | tests + live run earlier |
| All 15 goals on dashboard with required fields | **PASS** | API + headless Chromium DOM test |
| Counters reconcile with database | **PASS** | tests + guardian 3 check |
| Live trading disabled | **PASS** | no order/withdraw code paths (source scan test), health reports false |
| BTCC specification / 250x / 500x verified | **BLOCKED** | no authoritative BTCC source retrievable; goals 2-4 remain '?' |
| Seven-day soak | **NOT TESTED** | no soak has run; docs/SOAK_LOG.md has no completed run |
| Any strategy qualified | **FAIL (by evidence)** | 0 qualified; none expected to be; this is not a defect |

Test totals: **112 passed, 3 skipped, 0 failed, 0 not executed other than the 3 skips** (skips are Windows-only tests that will run on the PC).
