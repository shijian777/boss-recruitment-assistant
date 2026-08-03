@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 请先运行 install.bat。
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m pytest
if errorlevel 1 goto :failed

python -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name BossInviter ^
  --additional-hooks-dir hooks ^
  --collect-submodules pywinauto ^
  --collect-all comtypes ^
  main.py
if errorlevel 1 goto :failed

copy /y config.json "dist\BossInviter\config.json" >nul
copy /y config.example.json "dist\BossInviter\config.example.json" >nul
copy /y README.md "dist\BossInviter\README.md" >nul
if not exist "dist\BossInviter\data" mkdir "dist\BossInviter\data"
if not exist "dist\BossInviter\logs" mkdir "dist\BossInviter\logs"
if not exist "dist\BossInviter\screenshots" mkdir "dist\BossInviter\screenshots"

powershell -NoProfile -Command "Compress-Archive -Path 'dist\BossInviter\*' -DestinationPath 'dist\BossInviter-Windows-x64.zip' -Force"
if errorlevel 1 goto :failed

echo.
echo 打包完成：dist\BossInviter-Windows-x64.zip
pause
exit /b 0

:failed
echo.
echo [错误] 打包失败，请查看上方错误信息。
pause
exit /b 1
