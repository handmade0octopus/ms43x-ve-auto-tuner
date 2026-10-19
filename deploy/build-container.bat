@echo off
rem Build and run the MS43X VE auto-tuner web app in Docker — nothing to install
rem but Docker itself. Sessions are RAM-only; no data is stored on this machine.
setlocal
cd /d "%~dp0.."
set PORT=%1
if "%PORT%"=="" set PORT=8080

docker build -f deploy\Dockerfile -t ms43x-ve-auto-tuner . || goto :err
docker rm -f ms43x-ve-auto-tuner 2>nul
docker run -d --name ms43x-ve-auto-tuner ^
  -p 127.0.0.1:%PORT%:8000 ^
  --read-only --tmpfs /tmp ^
  --restart unless-stopped ^
  ms43x-ve-auto-tuner || goto :err

echo.
echo MS43X VE auto-tuner is running:  http://localhost:%PORT%
echo stop it with:  docker rm -f ms43x-ve-auto-tuner
goto :eof
:err
echo BUILD/RUN FAILED
exit /b 1
