@echo off
rem Portable fake adb for tests: prefer project .venv python, else python from PATH
set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" "%~dp0fake_adb.py" %1 %2 %3 "%~4" %5 %6 %7 %8 %9
