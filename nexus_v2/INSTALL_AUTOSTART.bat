@echo off
setlocal
call "%~dp0scripts\nexus_env.bat" || exit /b 1
pushd "%NEXUS_ROOT%"
echo [NEXUS] Registering a per-user Scheduled Task (runs START_NEXUS.bat at sign-in, no admin rights, no time limit)...
"%NEXUS_PY%" -m nexus install-startup
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
