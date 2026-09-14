FROM python:3.14-slim

ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="tgbot_voxcpm" \
      org.opencontainers.image.description="Telegram bot for VoxCPM voice cloning" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py voxcpm_client.py ./

RUN mkdir -p /app/data

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
