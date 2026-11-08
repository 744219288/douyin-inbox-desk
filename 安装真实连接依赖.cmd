@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements-live.lock.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "from desk.transport import load_vendor; load_vendor(); print('私信依赖检查通过')"
if errorlevel 1 goto fail
echo 安装完成。请运行扫码登录.cmd。
pause
exit /b 0
:fail
echo 安装失败，请查看上方错误。未修改全局 Python 环境。
pause
exit /b 1
