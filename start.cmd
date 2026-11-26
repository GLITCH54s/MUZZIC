@echo off
rem MUZZIC launcher for Windows: creates .venv on first run, then starts the app.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" server.py %*
goto :eof

:fail
echo.
echo Setup failed. Install Python 3.10+ from https://www.python.org/downloads/
echo and make sure "python" is on PATH, then run start.cmd again.
pause