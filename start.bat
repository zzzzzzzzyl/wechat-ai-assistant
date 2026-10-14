@echo off
REM ---------------------------------------------------------------------------
REM  WeChat AI Assistant - launcher
REM
REM  NOTE: this file is intentionally ASCII-only.
REM  cmd.exe parses .bat using the system codepage (GBK on zh-CN Windows),
REM  so a UTF-8 file containing Chinese gets mangled and breaks parsing
REM  (verified: neighbouring ASCII lines got eaten too).
REM  Chinese messages are printed by Python instead, after "chcp 65001".
REM ---------------------------------------------------------------------------

chcp 65001 >nul
title WeChat AI Assistant (read-only)
cd /d "%~dp0"

REM   Force UTF-8 for Python IO. Without this Python uses the locale encoding
REM   (GBK on zh-CN), and printing a chat name containing an unrepresentable
REM   character (superscript 4, emoji, ...) crashes with UnicodeEncodeError.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo  ==================================================
echo    WeChat AI Assistant   -   read-only, never sends
echo  ==================================================
echo.

REM ---------- 1. Python ----------
python --version >nul 2>nul
if errorlevel 1 (
    echo  [x] Python not found.
    echo      Install Python 3.9+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [ok] %PYVER%

REM ---------- 2. Dependencies ----------
python -c "import uiautomation, win32gui, PIL, numpy, yaml" >nul 2>nul
if errorlevel 1 (
    echo  [x] Missing dependencies. Run:
    echo      pip install uiautomation pywin32 pillow numpy pyyaml PySide6
    echo.
    pause
    exit /b 1
)
echo  [ok] core dependencies

REM   PySide6 drives the Qt UI (the default since 2026-09-26).
REM   WARNING: without it run_watch.py SILENTLY FALLS BACK to the old Tk UI
REM   (see run_watch.py, the ui.backend loop). The app still runs, so the only
REM   symptom is "the window looks like the old version" - easy to misread as
REM   a stale script. So check it here, out loud.
REM
REM   NOTE: no parentheses in these echo lines on purpose. Inside an "if (...)"
REM   block cmd treats ")" as closing the block, and caret-escaping "^(...^)"
REM   does NOT help there - verified: it broke the whole script with
REM   "one. was unexpected at this time". Hence the flat goto style below.
python -c "import PySide6" >nul 2>nul
if errorlevel 1 goto no_qt
for /f "delims=" %%v in ('python -c "import PySide6;print(PySide6.__version__)" 2^>^&1') do set QTVER=%%v
echo  [ok] PySide6 %QTVER% - Qt UI
goto qt_done

:no_qt
echo  [!] PySide6 NOT installed - the UI will fall back to the OLD Tk one.
echo      Install it with:  pip install PySide6
echo      Use PySide6-Essentials instead if you want a smaller download.
echo.

:qt_done

python -c "import rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 goto no_ocr
goto ocr_done

:no_ocr
echo  [!] rapidocr-onnxruntime not installed
echo      - group chat sender nicknames will be unavailable
echo      - install with: pip install rapidocr-onnxruntime

:ocr_done

REM ---------- 3. Leftover instance ----------
REM   Handled by run_watch.py itself, via a NAMED MUTEX (see
REM   single_instance_guard). That is the only reliable way: a leftover instance
REM   keeps the global hotkey registered, and "is python.exe running" cannot
REM   tell our process from any other Python script.
REM   (Do NOT try to inspect process command lines here either - the classic
REM   tool for that is gone on Windows 11; verified absent on this machine.)

REM ---------- 4. Config ----------
if not exist "config.yaml" (
    echo  [x] config.yaml not found. Run this from the project folder.
    echo.
    pause
    exit /b 1
)
if not exist "secrets.yaml" (
    echo  [!] secrets.yaml not found
    echo      - AI generation is disabled; the tool will only READ chats.
    echo      - Create secrets.yaml containing:
    echo            ai:
    echo              api_key: "sk-your-key"
    echo.
)

REM ---------- 5. Launch ----------
echo.
echo  --------------------------------------------------
echo    Ctrl+Alt+Q     read current chat + generate replies
echo    Window buttons:
echo      Generate      read + ask the AI for 3 replies
echo      Read-only     just read the chat (no tokens spent)
echo      Show prompt   what was actually sent to the model
echo      Settings      UI + AI + persona + per-chat overrides
echo    Ctrl+C         quit
echo  --------------------------------------------------
echo    UI backend: qt is the default; set ui.backend: tk in config.yaml
echo    to fall back to the old Tk window. The startup log says which one ran.
echo  --------------------------------------------------
echo.

python -u run_watch.py %*

echo.
echo  Exited.
pause
