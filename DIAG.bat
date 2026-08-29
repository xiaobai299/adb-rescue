@echo off
cd /d "%~dp0"
title ADB Rescue - Diagnostics
echo ============================================
echo   Diagnostics
echo ============================================
echo.

set "PYEXE="
if not defined PYEXE call :try "%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE (
  python -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)

echo --- 1. Python ---
if defined PYEXE (
  echo Selected : %PYEXE%
  %PYEXE% -c "import sys,tkinter,PIL;print('  version : '+sys.version.split()[0]);print('  tkinter : '+str(tkinter.TkVersion));print('  Pillow  : '+PIL.__version__)" 2>&1
) else (
  echo [FAIL] no Python with tkinter
)
echo.

echo --- 2. adb ---
if exist "%~dp0tools\adb.exe" (
  echo Found    : %~dp0tools\adb.exe
  "%~dp0tools\adb.exe" version 2>&1
) else (
  where adb >nul 2>nul
  if errorlevel 1 (
    echo [FAIL] adb not found
  ) else (
    echo Found in PATH
    adb version 2>&1
  )
)
echo.

echo --- 3. Devices ---
if exist "%~dp0tools\adb.exe" (
  "%~dp0tools\adb.exe" devices -l 2>&1
) else (
  adb devices -l 2>&1
)
echo.
echo ============================================
pause
exit /b 0

:try
if "%~1"=="" exit /b 1
if not exist %1 exit /b 1
%1 -c "import tkinter" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%~1"
exit /b 0
