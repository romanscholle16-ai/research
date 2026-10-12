@echo off
REM NEXUS V2 launcher. Starts the supervisor detached (idempotent: a second call is refused by the lock).
cd /d "%~dp0\.."
python -m nexus start
