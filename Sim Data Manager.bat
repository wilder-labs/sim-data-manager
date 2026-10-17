@echo off
cd /d "%~dp0"
if not exist "python\pythonw.exe" (
  echo The python folder is missing. Re-copy the whole Sim Data Manager folder.
  pause
  exit /b 1
)
start "" "python\pythonw.exe" run_sdm.py
