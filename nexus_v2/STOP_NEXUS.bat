@echo off
setlocal
call "%~dp0scripts\nexus_env.bat" || exit /b 1
pushd "%NEXUS_ROOT%"
echo [NEXUS] Requesting graceful shutdown (workers finish their current stage; nothing is deleted)...
"%NEXUS_PY%" -m nexus shutdown
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
