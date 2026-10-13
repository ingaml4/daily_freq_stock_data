FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    EMSCAN_STORE_PATH=/data/market.sqlite \
    EMSCAN_TZ=Asia/Shanghai

# 证书通过环境变量注入，不要打进镜像：
#   docker run -e MAIRUI_LICENCE=... -v /srv/emscan/data:/data emscan:0.1 run

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY emscan ./emscan
# config.yaml 是可选的：全部配置都有默认值，licence 走 MAIRUI_LICENCE 即可
COPY config.yaml* ./

RUN mkdir -p /data /app/logs

ENTRYPOINT ["python3", "-m", "emscan"]
CMD ["run"]
