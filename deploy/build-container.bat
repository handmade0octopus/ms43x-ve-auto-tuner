@echo off
rem Build and run the MS43 VE Auto-Tune web app in Docker — nothing to install
rem but Docker itself. Sessions are RAM-only; no data is stored on this machine.
setlocal
cd /d "%~dp0.."
set PORT=%1
if "%PORT%"=="" set PORT=8080

docker build -f deploy\Dockerfile -t ms43-ve-autotune . || goto :err
docker rm -f ms43-ve-autotune 2>nul
docker run -d --name ms43-ve-autotune ^
  -p 127.0.0.1:%PORT%:8000 ^
  --read-only --tmpfs /tmp ^
  --restart unless-stopped ^
  ms43-ve-autotune || goto :err

echo.
echo MS43 VE Auto-Tune is running:  http://localhost:%PORT%
echo stop it with:  docker rm -f ms43-ve-autotune
goto :eof
:err
echo BUILD/RUN FAILED
exit /b 1
