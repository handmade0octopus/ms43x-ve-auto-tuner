@echo off
rem Build the standalone MS43VEAutoTune.exe (app window + local server).
rem Requires: Python 3.12+ on PATH with `pip install -r requirements.txt pyinstaller`
setlocal
python -m PyInstaller --onefile --noconsole server.py -i icon.ico -n MS43VEAutoTune ^
  --add-data "web;web" --collect-submodules uvicorn --collect-submodules webview -y
if errorlevel 1 (echo BUILD FAILED & exit /b 1)
echo.
echo Done: dist\MS43VEAutoTune.exe  (double-click: app window opens; closing it stops the server)
pause
