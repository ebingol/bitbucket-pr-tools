@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python launcher.py stats %*
) else (
  py -3 launcher.py stats %*
)
set "result=%errorlevel%"
pause
exit /b %result%
