@echo off
REM ============================================================
REM  xianyu-rss launcher - portable Python edition
REM  ASCII-only on purpose (avoids GBK/UTF-8 mojibake in cmd)
REM  Usage: run.cmd [login|all|once|loop|serve|check|diag] [extra args...]
REM         extra args are forwarded to the script, e.g.
REM           run.cmd login --no-verify
REM           run.cmd once --keyword macmini
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
echo  diag  = troubleshoot (why 0 items / RGV587)
echo.
echo  FreshRSS subscribe:
echo    http://^<this-pc-ip^>:8899/macmini.xml
echo.
echo  Extra args are forwarded, e.g.:
echo    run.cmd login --no-verify
echo    run.cmd once --keyword macmini
echo.

REM NOTE: cmd does NOT update %%* after shift, and percent expansion inside a
REM       parenthesised block happens once at parse time (so shift looks
REM       "broken" there).  Dispatch via labels and forward args positionally.
if /i "%~1"=="login" goto do_login
if /i "%~1"=="diag" goto do_diag
if "%~1"=="" goto do_all
goto do_cmd

:do_login
shift
"%PY%" xianyu_login.py %1 %2 %3 %4 %5 %6 %7 %8 %9
goto done

:do_diag
"%PY%" diag.py %1 %2 %3 %4 %5 %6 %7 %8 %9
goto done

:do_all
"%PY%" xianyu_rss.py all
goto done

:do_cmd
"%PY%" xianyu_rss.py %~1 %1 %2 %3 %4 %5 %6 %7 %8 %9
goto done

:done
echo.
echo [stopped] press any key to close ...
pause >nul
endlocal
