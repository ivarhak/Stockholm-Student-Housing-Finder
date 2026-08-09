@echo off
rem Windows equivalent of start.command — double-click it, or run start.bat from
rem a terminal. Sets up the venv and dependencies on first run, then serves the
rem dashboard and opens it in your browser. Arguments pass straight through.

setlocal
cd /d "%~dp0"

set VENV=venv
set STAMP=%VENV%\.requirements-installed

where py >nul 2>&1 && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.10+ not found. Install it from python.org/downloads
  echo and tick "Add Python to PATH" during setup.
  pause
  exit /b 1
)

if not exist "%VENV%" (
  echo First run - creating a virtual environment in .\%VENV%
  %PY% -m venv "%VENV%" || (pause & exit /b 1)
)

rem Reinstall only when requirements.txt actually changes.
for /f "delims=" %%H in ('certutil -hashfile requirements.txt MD5 ^| findstr /r "^[0-9a-f]"') do set WANT=%%H
set HAVE=
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
if not "%HAVE%"=="%WANT%" (
  echo Installing dependencies ^(once - this takes a minute^)
  rem Upgrade pip first: a venv made by an older Python or by an IDE can carry a
  rem pip too old for the interpreter running it, which fails on pkgutil.ImpImporter.
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
  >"%STAMP%" echo %WANT%
)

echo Starting - the dashboard will open in your browser.
echo The first scrape takes about a minute. Press Ctrl+C here to stop.
echo.
"%VENV%\Scripts\python.exe" monitor.py %*
if errorlevel 1 pause
