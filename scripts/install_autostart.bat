@echo off
REM OPTIONAL: run the research service at Windows logon (re-enter with Settings > autostart). Remove with: schtasks /Delete /TN StrategyLabService /F
cd /d "%~dp0\.."
schtasks /Create /TN StrategyLabService /SC ONLOGON /TR "\"%CD%\.venv\Scripts\pythonw.exe\" -m strategylab.service" /F
echo Autostart task created. Research only resumes if it was left in 'running' state (control state is persisted).
