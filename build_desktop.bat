@echo off
REM ============================================================
REM A股实时看盘 —— 桌面版一键打包脚本（Windows）
REM
REM   build_desktop.bat           打包
REM   build_desktop.bat --run     打包并立即启动
REM   build_desktop.bat --clean   先清理 build\
REM
REM 产物: dist\AStockDashboard\AStockDashboard.exe
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set VENV=%~dp0.venv-desktop
set PY=%VENV%\Scripts\python.exe

REM ---------- 1. 检查 / 创建隔离环境 ----------
if not exist "%PY%" (
  echo [>] 创建隔离环境 %VENV%
  python -m venv "%VENV%"
  "%PY%" -m pip install --quiet --upgrade pip
)

REM ---------- 2. 安装依赖 ----------
"%PY%" -c "import PyInstaller, webview" 2>nul || (
  echo [>] 安装 pyinstaller + pywebview
  "%PY%" -m pip install --quiet pyinstaller pywebview
)

REM ---------- 3. 确保页面是最新的（ECharts 内联）----------
echo [>] 生成自包含页面
"%PY%" build_inline.py

REM ---------- 4. 打包 ----------
if /i "%~1"=="--clean" (
  echo [>] 清理 build\
  if exist build rmdir /s /q build
)

REM PyInstaller 覆盖已有 dist 会先删除，用 move 挪开避免大批量删除被拦截
if exist dist (
  set STAMP=%RANDOM%
  echo [>] 备份旧产物到 %TEMP%\kp_dist_%STAMP%
  move dist "%TEMP%\kp_dist_%STAMP%" >nul 2>&1
)

echo [>] 执行 PyInstaller 打包（约 15~40 秒）
"%VENV%\Scripts\pyinstaller.exe" --noconfirm desktop_app.spec
if errorlevel 1 (
  echo [!] 打包失败
  exit /b 1
)

REM ---------- 5. 输出结果 ----------
echo.
echo [OK] 打包完成
for /f "tokens=1,2" %%a in ('powershell -NoProfile -Command ^
  "(Get-ChildItem -Recurse dist\AStockDashboard -File | Measure-Object Length -Sum).Sum"') do set SIZE=%%a
if defined SIZE (
  set MB=!SIZE!
  set /a MB=!MB!/1024/1024
  echo     产物: dist\AStockDashboard\AStockDashboard.exe  (!MB! MB)
)
echo     运行: dist\AStockDashboard\AStockDashboard.exe
echo     日志: %%APPDATA%%\AStockDashboard\app.log
echo.
echo     提示: 若窗口空白/无法显示，请安装 WebView2 Runtime
echo           https://developer.microsoft.com/microsoft-edge/webview2/

if /i "%~1"=="--run" (
  echo [>] 启动桌面应用
  start "" "dist\AStockDashboard\AStockDashboard.exe"
)

endlocal
