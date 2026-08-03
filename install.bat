@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python。请安装 Python 3.11 或 3.12，并勾选 Add Python to PATH。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 goto :failed
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist data mkdir data
if not exist logs mkdir logs
if not exist screenshots mkdir screenshots

echo.
echo 安装完成。请先启动并登录 BOSS 直聘 Windows 客户端，再运行 run.bat。
pause
exit /b 0

:failed
echo.
echo [错误] 安装失败，请查看上方错误信息。
pause
exit /b 1
