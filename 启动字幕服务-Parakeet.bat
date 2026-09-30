@echo off
rem Same as the normal launcher, but recognition uses Parakeet-ja
rem (Japanese only, more accurate, about 3x the CPU of SenseVoice).
rem ASCII only on purpose, see the note in the normal launcher.
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\text\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -u tools\run_service.py --asr parakeet %*
echo.
echo Service exited. Press any key to close.
pause >nul
