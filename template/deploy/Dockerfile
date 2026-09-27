# Oracle Cloud Always Free runs Ampere A1, which is ARM64. Everything here has
# arm64 wheels on Python 3.11, so the image builds natively on the VM with no
# emulation and no compiler toolchain.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=America/New_York
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r deploy/requirements.txt

COPY core/ /app/core/
COPY orb_breakout/ /app/orb_breakout/
COPY news_catalyst_options/ /app/news_catalyst_options/
COPY lvl_confluence/ /app/lvl_confluence/
COPY deploy/keepalive.py deploy/entrypoint.sh /app/deploy/
RUN chmod +x /app/deploy/entrypoint.sh

ENV PYTHONPATH=/app:/app/core BOT_STATE_DIR=/app/state
RUN mkdir -p /app/state
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
