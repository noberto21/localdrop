@echo off
setlocal
cd /d "%~dp0"
title LocalDrop

rem --- find Python 3 ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

rem --- one-time setup: private virtual environment + dependencies ---
if exist ".venv\Scripts\python.exe" goto havevenv
echo Creating virtual environment...
%PY% -m venv .venv
if errorlevel 1 goto fail
:havevenv
if exist ".venv\.deps-ok" if /i not "%~1"=="--update" goto run
echo Installing dependencies (needs internet once)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
echo ok> ".venv\.deps-ok"

:run
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
exit /b 0

:nopython
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
