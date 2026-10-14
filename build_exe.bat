@echo off
REM ---------------------------------------------------------------------------
REM  Build the Windows bundle (PyInstaller, onedir).
REM
REM  NOTE: ASCII only on purpose. cmd.exe parses .bat with the system codepage
REM  (GBK on zh-CN Windows); a UTF-8 file containing Chinese gets mangled and
REM  eats neighbouring ASCII lines too. All Chinese output comes from Python.
REM
REM  Output:  dist\<APPNAME>\        <- the whole folder, zip it and ship it
REM ---------------------------------------------------------------------------

chcp 65001 >nul
title Build WeChat AI Assistant
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo  ==================================================
echo    WeChat AI Assistant  -  build (onedir)
echo  ==================================================
echo.

REM ---------- 1. PyInstaller ----------
REM   Normally "pip install pyinstaller". If it is not on the global
REM   site-packages (e.g. installing there is not permitted), fall back to the
REM   workspace-local copy in .packtools\ that the agent used.
python -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto have_pyinstaller

if not exist ".packtools\PyInstaller\__init__.py" goto no_pyinstaller
set PYTHONPATH=%CD%\.packtools;%PYTHONPATH%
python -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto have_pyinstaller

:no_pyinstaller
echo  [x] PyInstaller not found.
echo      Install it with:   pip install pyinstaller
echo.
pause
exit /b 1

:have_pyinstaller
for /f "delims=" %%v in ('python -m PyInstaller --version 2^>^&1') do set PIVER=%%v
echo  [ok] PyInstaller %PIVER%

REM ---------- 2. Sanity checks on the inputs ----------
python -c "import PySide6" >nul 2>nul
if errorlevel 1 goto no_qt
echo  [ok] PySide6 present (the Qt UI is the only UI since 2026-09-26)
goto qt_done

:no_qt
echo  [x] PySide6 is NOT installed - the bundle would be useless.
echo      pip install PySide6
echo.
pause
exit /b 1

:qt_done

if exist "secrets.yaml" (
    echo  [!] secrets.yaml EXISTS in this folder.
    echo      It is NOT bundled ^(the spec never copies it^), but double-check
    echo      the generated dist folder before sending it to anyone.
    echo.
)

if not exist "wechat_assistant\assets\icon.ico" (
    echo  [!] wechat_assistant\assets\icon.ico missing
    echo      Regenerate with:  python probes\make_icon.py ^<source.png^>
    echo.
)

REM ---------- 3. Build ----------
REM   Two ways to speed this up on rebuilds: the "build\" cache folder is
REM   reused automatically (do not delete it unless something looks stale).
rmdir /s /q "dist\%~n0" >nul 2>nul

python -m PyInstaller wechat_ai.spec --noconfirm --clean
if errorlevel 1 goto build_failed

echo.
echo  [ok] Built: dist\ (see the folder listing below)
dir /b "dist"
echo.
echo  ------------------------------------------------------------------
echo   Next steps:
echo     1. Run  dist\WeChatAI...\*.exe  and check it starts.
echo     2. If it does not start, read the log next to the exe
echo        (logs\app.log) - the windowed build has no console.
echo     3. Zip the WHOLE folder (not just the .exe) and send that.
echo  ------------------------------------------------------------------
echo.
pause
exit /b 0

:build_failed
echo.
echo  [x] Build failed - see the PyInstaller output above.
echo.
pause
exit /b 1
