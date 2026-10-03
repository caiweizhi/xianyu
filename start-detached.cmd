@echo off
REM ============================================================
REM  Detached launcher for xianyu-rss (portable Python edition)
REM  Starts the resident monitor in its own minimized window,
REM  detached from the caller, so closing the terminal does NOT
REM  kill it.  Logs go to  logs\run.log
REM
REM  stop:  taskkill /FI "WINDOWTITLE eq xianyu-rss*" /F
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY_BASE=D:\Program Files\python"
set "PY=%PY_BASE%\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONHOME=%PY_BASE%"
set "PATH=%PY_BASE%;%PY_BASE%\Scripts;%PATH%"
set "PYTHONIOENCODING=utf-8"

if not exist logs mkdir logs

start "xianyu-rss" /min cmd /c ""%PY%" xianyu_rss.py all >"%CD%\logs\run.log" 2>&1"

echo started.
echo   log  : %CD%\logs\run.log
echo   stop : taskkill /FI "WINDOWTITLE eq xianyu-rss*" /F
endlocal
