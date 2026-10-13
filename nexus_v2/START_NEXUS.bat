@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ============================================================================================
rem  START_NEXUS.bat - one-command installer + launcher for NEXUS V2 (research only; live trading is impossible).
rem  Steps: locate install dir -> find Python -> create/repair venv -> install deps -> validate config/DB ->
rem         refuse duplicate supervisor -> launch detached supervisor -> VERIFY health + progress -> open dashboard.
rem  Exit codes: 0 verified healthy | 1 launch/verification failed | 2 preflight refused | 3 no usable Python | 4 dependency install failed
rem  Optional args: --autostart (no browser)  --no-browser  --profile NAME   (default profile: unattended_7_day)
rem ============================================================================================
set "NEXUS_ROOT=%~dp0"
if "%NEXUS_ROOT:~-1%"=="\" set "NEXUS_ROOT=%NEXUS_ROOT:~0,-1%"
pushd "%NEXUS_ROOT%" || (echo [NEXUS] cannot enter "%NEXUS_ROOT%" & exit /b 1)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "var\logs" mkdir "var\logs"
set "LOG=%NEXUS_ROOT%\var\logs\launcher.log"
call :log "==== START_NEXUS %DATE% %TIME% root=%NEXUS_ROOT% args=%*"

rem ---- 1. find a base Python (3.10+; 3.12 preferred) ------------------------------------------------
set "BASEPY="
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined BASEPY (
    py -%%V -c "import sys" >nul 2>&1 && set "BASEPY=py -%%V"
  )
)
if not defined BASEPY (
  python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "BASEPY=python"
)
set "VENVPY=%NEXUS_ROOT%\.venv\Scripts\python.exe"

rem ---- 2. create or repair the virtual environment ---------------------------------------------------
if exist "%VENVPY%" (
  "%VENVPY%" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    call :log "venv interpreter is broken; moving it aside and recreating"
    move /y ".venv" ".venv.broken-%RANDOM%" >nul 2>&1
  )
)
if not exist "%VENVPY%" (
  if not defined BASEPY (
    call :log "ERROR: no Python 3.10+ found. Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add to PATH' or keep the py launcher) and run again."
    popd & exit /b 3
  )
  call :log "creating virtual environment with !BASEPY!"
  !BASEPY! -m venv ".venv" >> "%LOG%" 2>&1
  if errorlevel 1 ( call :log "ERROR: venv creation failed (see launcher.log)" & popd & exit /b 3 )
)

rem ---- 3. dependencies (numpy only for runtime) -------------------------------------------------------
"%VENVPY%" -c "import numpy" >nul 2>&1
if errorlevel 1 (
  call :log "installing dependencies from requirements-runtime.txt"
  "%VENVPY%" -m pip install --disable-pip-version-check -r "%NEXUS_ROOT%\requirements-runtime.txt" >> "%LOG%" 2>&1
  if errorlevel 1 ( call :log "ERROR: dependency install failed (network/proxy?). See var\logs\launcher.log" & popd & exit /b 4 )
)

rem ---- 4. launch + verify (Python does config/DB validation, duplicate prevention and real health checks) --
call :log "running: nexus launch %*"
"%VENVPY%" -m nexus launch %*
set "RC=%ERRORLEVEL%"
call :log "launch finished with exit code %RC%"
if not "%RC%"=="0" echo [NEXUS] FAILED (exit %RC%). Details: var\logs\launcher.log  and  CHECK_NEXUS.bat
popd
exit /b %RC%

:log
echo [NEXUS] %~1
>> "%LOG%" echo %DATE% %TIME% %~1
exit /b 0
