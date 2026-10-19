#!/usr/bin/env sh
# Build and run the MS43 VE Auto-Tune web app in Docker — nothing to install
# but Docker itself. Sessions are RAM-only; no data is stored on this machine.
set -e
cd "$(dirname "$0")/.."
PORT="${1:-8080}"

docker build -f deploy/Dockerfile -t ms43-ve-autotune .

docker rm -f ms43-ve-autotune 2>/dev/null || true
docker run -d --name ms43-ve-autotune \
  -p "127.0.0.1:${PORT}:8000" \
  --read-only --tmpfs /tmp \
  --restart unless-stopped \
  ms43-ve-autotune

echo
echo "MS43 VE Auto-Tune is running:  http://localhost:${PORT}"
echo "stop it with:  docker rm -f ms43-ve-autotune"
