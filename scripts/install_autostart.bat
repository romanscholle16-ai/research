@echo off
REM Installs two Task Scheduler entries (NOT TESTED ON WINDOWS from the build environment - verify after install):
REM  1) at logon, start the research service; 2) every 10 minutes, start it again (a pid lock makes extra starts exit immediately),
REM  so a crashed service comes back without anyone opening the dashboard. Research only runs if control state is 'running'.
REM Remove with:  schtasks /Delete /TN StrategyLabService /F  &  schtasks /Delete /TN StrategyLabWatchdog /F
cd /d "%~dp0\.."
set PY="%CD%\.venv\Scripts\pythonw.exe"
schtasks /Create /TN StrategyLabService /SC ONLOGON /TR "%PY% -m strategylab.service" /F
schtasks /Create /TN StrategyLabWatchdog /SC MINUTE /MO 10 /TR "%PY% -m strategylab.service" /F
echo Done. Check Task Scheduler > StrategyLabService / StrategyLabWatchdog.
