# -*- coding: utf-8 -*-
"""生成 启动.bat / 诊断.bat。

要点（Windows 批处理的两个硬性要求）：
  1) 必须用 CRLF 行尾，LF 会导致 cmd 解析异常、窗口一闪而过
  2) 中文 Windows 的 cmd 默认按 GBK 解析，UTF-8 无 BOM 会变乱码甚至解析失败
因此这里统一以 gbk 编码 + CRLF 写入。控制台提示语用英文，
避免任何代码页问题；图形界面本身是全中文的。
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LAUNCHER = r'''@echo off
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
'''

DIAG = r'''@echo off
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
'''


def write_gbk_crlf(name: str, text: str) -> str:
    path = os.path.join(ROOT, name)
    with open(path, "w", encoding="gbk", newline="\r\n") as fp:
        fp.write(text)
    return path


if __name__ == "__main__":
    p1 = write_gbk_crlf("启动.bat", LAUNCHER)
    p2 = write_gbk_crlf("DIAG.bat", DIAG)
    for p in (p1, p2):
        raw = open(p, "rb").read()
        print(f"{p}: {len(raw)} bytes, CRLF={raw.count(b'\r\n')}, 裸LF={raw.count(b'\n') - raw.count(b'\r\n')}")
