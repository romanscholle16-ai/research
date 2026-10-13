@echo off
setlocal
call "%~dp0scripts\nexus_env.bat" || exit /b 1
pushd "%NEXUS_ROOT%"
"%NEXUS_PY%" -m nexus uninstall-startup
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
