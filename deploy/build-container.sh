#!/usr/bin/env sh
# Build and run the MS43X VE auto-tuner web app in Docker — nothing to install
# but Docker itself. Sessions are RAM-only; no data is stored on this machine.
set -e
cd "$(dirname "$0")/.."
PORT="${1:-8080}"

docker build -f deploy/Dockerfile -t ms43x-ve-auto-tuner .

docker rm -f ms43x-ve-auto-tuner 2>/dev/null || true
docker run -d --name ms43x-ve-auto-tuner \
  -p "127.0.0.1:${PORT}:8000" \
  --read-only --tmpfs /tmp \
  --restart unless-stopped \
  ms43x-ve-auto-tuner

echo
echo "MS43X VE auto-tuner is running:  http://localhost:${PORT}"
echo "stop it with:  docker rm -f ms43x-ve-auto-tuner"
