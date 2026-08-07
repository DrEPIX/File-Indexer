@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "File Indexer V1.pyw"
  exit /b 0
)
start "" pythonw "File Indexer V1.pyw"
