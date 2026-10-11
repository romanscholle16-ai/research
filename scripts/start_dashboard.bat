@echo off
REM Starts the dashboard (which launches the background research service). Double-click to use.
cd /d "%~dp0\.."
if not exist .venv ( python -m venv .venv & .venv\Scripts\pip install -r requirements.txt )
.venv\Scripts\python -m strategylab.dashboard
