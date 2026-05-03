@echo off
setlocal
cd /d "%~dp0"

set "ELEVATED="
if /I "%~1"=="--elevated" (
  set "ELEVATED=1"
  shift
)

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  if defined ELEVATED (
    echo Administrator permission was not granted.
    pause
    exit /b 1
  )

  echo Requesting administrator permission...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated %*' -WorkingDirectory '%~dp0' -Verb RunAs"
  if errorlevel 1 (
    echo Could not request administrator permission.
    pause
  )
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found at .venv\Scripts\python.exe
  echo Create it and install requirements first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo Checking for an old web app on port 7500...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$connections = Get-NetTCPConnection -LocalPort 7500 -State Listen -ErrorAction SilentlyContinue; foreach ($connection in $connections) { if ($connection.OwningProcess -and $connection.OwningProcess -ne $PID) { Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue } }"

if "%~1"=="" (
  ".venv\Scripts\python.exe" web_app.py
) else (
  ".venv\Scripts\python.exe" web_app.py %*
)

if errorlevel 1 (
  echo.
  echo Web app stopped with an error.
  pause
)
