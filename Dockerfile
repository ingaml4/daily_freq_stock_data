FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    EMSCAN_STORE_PATH=/data/market.sqlite

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY emscan ./emscan
COPY config.yaml ./

RUN mkdir -p /data /app/logs

ENTRYPOINT ["python3", "-m", "emscan"]
CMD ["run"]
