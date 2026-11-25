@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Shock Timer is not set up yet.
  echo Run setup-windows.bat first.
  pause
  exit /b 1
)
start "Shock Timer" /b ".venv\Scripts\pythonw.exe" "%~dp0shock_timer_windows.py"
