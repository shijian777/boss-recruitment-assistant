@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 尚未安装依赖，请先运行 install.bat。
  pause
  exit /b 1
)

if not exist data mkdir data
if not exist logs mkdir logs
if not exist screenshots mkdir screenshots

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
  echo.
  echo 程序异常退出，请检查 logs\boss_inviter.log。
  pause
)
