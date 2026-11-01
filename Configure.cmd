@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0notion-workflows\configure_workspace.py"
pause
