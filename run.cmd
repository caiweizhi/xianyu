@echo off
REM ============================================================
REM  xianyu-rss launcher - portable Python edition
REM  ASCII-only on purpose (avoids GBK/UTF-8 mojibake in cmd)
REM  Usage: run.cmd [login|all|once|loop|serve|check]
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY_BASE=D:\Program Files\python"
set "PY=%PY_BASE%\python.exe"

if not exist "%PY%" (
    set "PY=python"
    echo [WARN] portable python not found at %PY_BASE%, fallback to system python
)

set "PYTHONHOME=%PY_BASE%"
set "PATH=%PY_BASE%;%PY_BASE%\Scripts;%PATH%"
set "PYTHONIOENCODING=utf-8"

echo ==========================================
echo  xianyu-rss  (stdlib only, no 3rd-party)
echo  python : %PY%
echo  dir    : %cd%
echo ==========================================
echo.
echo  login = scan QR code to (re)login Xianyu
echo  all   = fetch loop + rss server  (default)
echo  once  = fetch once and exit
echo  check = verify cookie/api
echo  serve = rss server only
echo.
echo  FreshRSS subscribe:
echo    http://^<this-pc-ip^>:8899/macmini.xml
echo.

if /i "%~1"=="login" (
    "%PY%" xianyu_login.py
) else if "%~1"=="" (
    "%PY%" xianyu_rss.py all
) else (
    "%PY%" xianyu_rss.py %~1
)

echo.
echo [stopped] press any key to close ...
pause >nul
endlocal
