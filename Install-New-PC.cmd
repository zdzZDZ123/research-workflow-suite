@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" -InstallApps
if errorlevel 1 echo Setup failed. Reopen this launcher after installing Python.
pause
