@echo off
rem Checks a roster.json against your sdm.db and writes match-report.json beside it.
rem Usage: drag a roster.json onto this file, or run:  "Check Roster.bat" path\to\roster.json path\to\sdm.db
setlocal
cd /d "%~dp0"
if "%~1"=="" (echo Drag a roster.json onto this file. & pause & exit /b 1)
set DB=%~2
if "%DB%"=="" set DB=sdm.db
python -I -m sdmmodern.residency check "%~1" --db "%DB%" --out "%~dpn1-match-report.json"
pause
