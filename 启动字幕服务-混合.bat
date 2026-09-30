@echo off
rem Same as the normal launcher, drafts use SenseVoice (fast) and
rem finals are re-decoded once with Parakeet-ja (accurate). Translation only uses finals.
rem ASCII only on purpose, see the note in the normal launcher.
chcp 65001 >nul
cd /d "%~dp0"

rem Python: project .venv first, then ..\..\.venv (original dev layout), then PATH.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -u tools\run_service.py --asr hybrid %*
echo.
echo Service exited. Press any key to close.
pause >nul
