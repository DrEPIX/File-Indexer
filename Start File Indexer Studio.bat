@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "File Indexer Studio.pyw"
) else (
  start "" pythonw "File Indexer Studio.pyw"
)
endlocal
