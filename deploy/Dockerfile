# MS43X VE auto-tuner — headless web server image.
# Build from the project root:  docker build -f deploy/Dockerfile -t ms43x-ve-auto-tuner .
# Browser-only sessions: all state lives in RAM per session, nothing is ever
# written to disk (no volume needed).
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY web/ web/
COPY server.py .

# Web mode: per-browser RAM sessions, browser uploads/downloads, no dialogs.
ENV VE_WEB=1 \
    VE_HOST=0.0.0.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 1000 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/recents', timeout=4)"]

CMD ["python", "-X", "utf8", "server.py", "--port", "8000", "--no-ui"]
