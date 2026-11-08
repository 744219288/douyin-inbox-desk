@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行安装真实连接依赖.cmd。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" login.py
pause
