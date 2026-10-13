@echo off
rem Shared environment for the NEXUS control scripts. Sets NEXUS_ROOT and NEXUS_PY (venv interpreter). Exits 1 if not installed.
set "NEXUS_ROOT=%~dp0.."
for %%I in ("%NEXUS_ROOT%") do set "NEXUS_ROOT=%%~fI"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "NEXUS_PY=%NEXUS_ROOT%\.venv\Scripts\python.exe"
if not exist "%NEXUS_PY%" (
  echo [NEXUS] Not installed yet: "%NEXUS_PY%" is missing. Run START_NEXUS.bat first.
  exit /b 1
)
exit /b 0
