@echo off
cd /d "%~dp0zotero-citation-sync"
"%~dp0.venv\Scripts\python.exe" -m citation_sync
pause
