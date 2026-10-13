@echo off
setlocal
call "%~dp0STOP_NEXUS.bat"
rem exit code 1 from STOP just means nothing was running; continue to start
call "%~dp0START_NEXUS.bat" %*
exit /b %ERRORLEVEL%
