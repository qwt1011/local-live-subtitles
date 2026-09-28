@echo off
rem ASCII only on purpose: cmd.exe parses .bat byte by byte using the active
rem code page, so UTF-8 Chinese text inside a batch file gets split into
rem garbage commands. All Chinese output lives in tools\run_service.py.
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\text\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -u tools\run_service.py %*
echo.
echo Service exited. Press any key to close.
pause >nul
