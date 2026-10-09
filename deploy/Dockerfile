# Trading Agent on a fixed-IP server.
# Build:  docker build -f deploy/Dockerfile -t trading-agent .
# Run:    see deploy/docker-compose.yml (dashboard on 127.0.0.1:8787, watch mode as its own service).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trading_agent ./trading_agent

# State (paper account, replays, caches, the cached Groww token) lives on a volume.
VOLUME ["/app/state"]
ENV STATE_DIR=/app/state

# Never run as root.
RUN useradd --create-home agent && chown -R agent /app
USER agent

CMD ["python", "-m", "trading_agent", "watch"]
