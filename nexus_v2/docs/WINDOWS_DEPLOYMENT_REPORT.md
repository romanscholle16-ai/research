# Windows deployment report

## Verdict: NOT DEPLOYED to a Windows computer — blocked by the execution environment
The shell Claude Code used for this assignment is a **Linux cloud VM**, not your Windows PC:
`Linux 6.18.44 x86_64`, not WSL, `CLAUDE_CODE_REMOTE=true`, no `cmd.exe`/`powershell.exe`/`schtasks.exe`, installation path
`/home/user/research/nexus_v2`. Per the assignment's own rule, a remote or temporary runtime does not count, so **nothing is installed
or running on your PC** and no runtime was (re)launched here either (the VM is ephemeral; its previous runtime had already ended).

## What was verified (on this Linux VM)
| Item | Result |
|---|---|
| Git | branch `claude/confident-gauss-l66zf7`; prior commit `38c603b` present; previous work, database (`var/nexus.db`) and datasets intact |
| Python | 3.13.16 (system); the Windows launcher targets 3.10–3.13 in `.venv` |
| Tests | **112 passed, 3 skipped, 0 failed** (260 s). The 3 skips are Windows-only tests (msvcrt lock, Scheduled Task round trip, detached start + CTRL_BREAK shutdown) |
| Database integrity | `PRAGMA integrity_check` ok (run by launcher/tests); baselines re-approved through change control (config hash unchanged; trusted-code hash updated for `config.py`) |
| Launcher logic | `nexus launch` / `check` verified end-to-end with real processes: supervisor lock + heartbeat, 5 service processes + workers with advancing heartbeats, DB write/read round trip, persisted progress, dashboard + health truthfulness, no critical incidents, duplicate-launch refusal, stopped runtime ⇒ exit 1 |
| Resource usage (Linux, 4 cores) | 2 workers ≈ 110 MB each, whole runtime ≈ 0.6 GB; 3–5% CPU at 150 experiments/agent/hour |
| Dashboard | http://127.0.0.1:8765 (when running); all 15 goals verified by API and headless-Chromium DOM tests |

## What was built for Windows (not executed on Windows)
* `START_NEXUS.bat` — finds Python (`py -3.12/3.13/3.11/3.10`, then `python`), creates/repairs `.venv`, installs `requirements-runtime.txt`
  (numpy only), then `python -m nexus launch --profile unattended_7_day` which validates config/DB/port, refuses a duplicate supervisor,
  starts the **detached** supervisor, and verifies health + progress before returning exit code 0. Logs to `var\logs\launcher.log`.
  Exit codes: 0 ok · 1 verification failed · 2 preflight refused · 3 no Python · 4 dependency install failed.
* `STOP_NEXUS.bat` (graceful, blocks until the supervisor exits), `RESTART_NEXUS.bat`, `CHECK_NEXUS.bat` (real health check + status),
  `INSTALL_AUTOSTART.bat` / `UNINSTALL_AUTOSTART.bat`.
* Auto-start = per-user Scheduled Task `NEXUS_V2_Research` created from XML: at logon (+1 min), least privilege (no admin), **no execution time limit**
  (the `schtasks` CLI default of 72 h would end a 7-day run), runs on battery, ignores duplicate triggers, then is read back and verified.
  Not configured anywhere yet.
* Windows-specific code: CPU via `GetSystemTimes`, per-process memory via `GetProcessMemoryInfo`, `BELOW_NORMAL` priority for research processes,
  per-process log files (Windows cannot rotate a file another process holds open), UTF-8 console, handle-closing dataset loads (atomic replace).
* Profile `unattended_7_day`: 2 workers, 150 experiments/agent/hour, memory throttle at 80% (keeps ≥3 GB for Windows), sustained-CPU throttle at 85%,
  5 GB disk reserve, 15-minute job stall judgement (heartbeat + progress based). It is a starting point; tune it from `CHECK_NEXUS.bat`/System Health.
  GPU (GTX 1070) is detected via `nvidia-smi` for display only; research does not use it.

## The smallest action needed from you
Run Claude Code (or just a terminal) **on the Windows PC** in a clone of this branch, then:
```
git clone -b claude/confident-gauss-l66zf7 https://github.com/romanscholle16-ai/research
cd research\nexus_v2
START_NEXUS.bat          :: first run downloads ~300 small checksum-verified data files (~4-10 min) - the launcher treats this as a legitimate wait
CHECK_NEXUS.bat
INSTALL_AUTOSTART.bat
python -m nexus test     :: via .venv\Scripts\python.exe; the 3 Windows-only tests will run for the first time
```
Also set Windows power plan to never sleep while on mains power (NEXUS will not change power settings). If you want the previous research history
(≈900 experiments, gitignored), copy this VM's `nexus_v2/var/` to the PC; otherwise a fresh database is created and everything is re-researched.
If a Windows-only test or `.bat` step fails, paste the output and it will be fixed — those paths are unproven.

## Known limitations
See `docs/KNOWN_LIMITATIONS.md` (BTCC unverified, proxy data, no candidate qualified, no soak).
