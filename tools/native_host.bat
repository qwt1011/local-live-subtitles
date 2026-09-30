@echo off
rem Chrome Native Messaging host for the extension (see tools\native_host.py).
rem stdout is the protocol channel: nothing else may be printed here.
set "PY=%~dp0..\..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=utf-8"
"%PY%" -u "%~dp0native_host.py" %*
