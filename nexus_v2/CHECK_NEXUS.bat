@echo off
setlocal
call "%~dp0scripts\nexus_env.bat" || exit /b 1
pushd "%NEXUS_ROOT%"
"%NEXUS_PY%" -m nexus check
set "RC=%ERRORLEVEL%"
echo.
"%NEXUS_PY%" -m nexus status
popd
exit /b %RC%
