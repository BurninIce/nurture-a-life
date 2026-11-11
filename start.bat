@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 以何物培育一颗心

rem ---- Python (project venv) ----
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE (
    py -3.13 -V >nul 2>&1
    if not errorlevel 1 set "PYEXE=py -3.13"
)
if not defined PYEXE set "PYEXE=python"

rem ---- 没装依赖先提醒一句（不替你装，装依赖要联网）----
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [提示] 还没有 .venv，先在本文件夹里跑这两条再双击本文件：
    echo        python -m venv .venv
    echo        .venv\Scripts\pip install -r requirements.txt
    echo.
)

rem ---- 第一次运行：先问 QQ 号和模型端点，写进 state\setup.json ----
if not exist "%~dp0state\setup.json" (
    %PYEXE% scripts\setup.py
    if not exist "%~dp0state\setup.json" (
        echo [提示] 设置没有完成，先不启动。双击本文件再来一次。
        pause
        exit /b 1
    )
    echo.
)

rem ---- run the orchestrator in THIS console (it is the main control window) ----
%PYEXE% scripts\launcher.py %*

echo.
echo ============================================================
echo   Launcher done. Keep the bot and NapCat windows open.
echo   Close this window anytime (children keep running).
echo ============================================================
pause
