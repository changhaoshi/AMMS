@echo off
REM ============================================================
REM A股实时看盘 —— 桌面版启动脚本（Windows，开发态）
REM
REM   start.bat              开发模式：开 GUI 窗口
REM   start.bat --headless   纯后端模式，不开窗口
REM
REM 分发版请用 build_desktop.bat 打包成 .exe
REM ============================================================
setlocal
cd /d "%~dp0"

set VENV=%~dp0.venv-desktop
set PY=%VENV%\Scripts\python.exe

REM 桌面版需要 pywebview；开发态也允许用系统 python（需已装 pywebview）
if not exist "%PY%" (
  python -c "import webview" 2>nul || (
    echo [>] 未检测到 pywebview，正在创建隔离环境
    python -m venv "%VENV%"
    "%PY%" -m pip install --quiet --upgrade pip
    "%PY%" -m pip install --quiet pywebview pyinstaller
  )
  if exist "%PY%" set PY=%VENV%\Scripts\python.exe
)

REM 首次运行先确保 index.html 是最新的
if not exist index.html (
  echo [>] 生成自包含页面
  "%PY%" build_inline.py
)

echo [>] 启动桌面应用
"%PY%" desktop_app.py %*
endlocal
