@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python launcher.py ready %*
) else (
  py -3 launcher.py ready %*
)
set "result=%errorlevel%"
pause
exit /b %result%
