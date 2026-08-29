@echo off
cd /d "%~dp0"
title ADB Rescue Tool

echo ============================================
echo   Android Phone Rescue Tool - ADB
echo ============================================
echo.
echo [1/4] Looking for a Python with tkinter...
echo.

set "PYEXE="
if not defined PYEXE call :try "%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE call :try "C:\Program Files\Python314\python.exe"
if not defined PYEXE call :try "C:\Program Files\Python313\python.exe"
if not defined PYEXE call :try "C:\Python313\python.exe"
if not defined PYEXE call :try "C:\Python312\python.exe"
if not defined PYEXE call :try "C:\Python311\python.exe"
if not defined PYEXE (
  python -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto nopython

echo [2/4] Python OK
echo       %PYEXE%
echo.

"%PYEXE%" -c "import PIL" >nul 2>nul
if errorlevel 1 goto installpillow
echo [3/4] Pillow OK
echo.
goto checkadb

:installpillow
echo [3/4] Installing Pillow, please wait...
"%PYEXE%" -m pip install -r requirements.txt
"%PYEXE%" -c "import PIL" >nul 2>nul
if errorlevel 1 goto nopillow
echo       Pillow installed
echo.

:checkadb
if exist "%~dp0tools\adb.exe" goto adbok
where adb >nul 2>nul
if errorlevel 1 goto adbwarn
echo [4/4] adb OK (system PATH)
echo.
goto start

:adbok
echo [4/4] adb OK (tools\adb.exe)
echo.
goto start

:adbwarn
echo [4/4] adb not found. You can set the path in Settings later.
echo.

:start
echo Starting the GUI...
echo.

"%PYEXE%" main.py

if errorlevel 1 (
  echo.
  echo [FAILED] The program exited with an error.
  echo Run   DIAG.bat   for a full diagnosis.
  echo.
  pause
)
goto :eof

:try
if "%~1"=="" exit /b 1
if not exist %1 exit /b 1
%1 -c "import tkinter" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%~1"
exit /b 0

:nopython
echo [ERROR] No usable Python found.
echo.
echo This tool needs Python 3.9+ WITH tkinter.
echo Your current python in PATH has no tkinter.
echo.
echo Fix: install Python from https://www.python.org/downloads/
echo      and check BOTH options:
echo        [x] Add python.exe to PATH
echo        [x] tcl/tk and IDLE
echo.
pause
exit /b 1

:nopillow
echo [ERROR] Pillow could not be installed.
echo Run this manually:
echo   "%PYEXE%" -m pip install Pillow
echo.
pause
exit /b 1
