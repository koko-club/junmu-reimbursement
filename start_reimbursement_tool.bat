@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 找不到 Python 3，请先安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)
python app.py
pause
