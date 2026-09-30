@echo off
rem Remove the one-click launch registry entry.
rem Writes only to HKCU (no admin needed). Safe to run again after moving the project folder.
rem ASCII only on purpose, see the note in the normal launcher.
chcp 65001 >nul
cd /d "%~dp0"

rem Python: project .venv first, then ..\..\.venv (original dev layout), then PATH.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" tools\install_native_host.py --uninstall
echo.
echo Press any key to close.
pause >nul
