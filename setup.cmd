@echo off
cd /d "%~dp0"
py -3.12 setup_environment.py
if errorlevel 1 (
 echo Setup failed. Python 3.12 x64 and an internet connection are required.
)
pause
