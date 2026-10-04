@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
 ".venv\Scripts\python.exe" launcher.py --restart
) else (
 python launcher.py --restart
)
if errorlevel 1 pause
