FROM python:3.12-slim-bookworm

ARG APP_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/app-home \
    APP_HOST=0.0.0.0 \
    APP_PORT=8800 \
    APP_DATA_DIR=/data \
    APP_SOFFICE_PATH=/usr/bin/soffice \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libreoffice-calc libreoffice-core fontconfig fonts-noto-cjk poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home app \
    && mkdir -p /data /backups /tmp/app-home \
    && chown app:app /data /backups \
    && chmod 1777 /tmp/app-home

WORKDIR /app
LABEL org.opencontainers.image.version="${APP_VERSION}"
COPY requirements.txt requirements-test.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -r requirements-test.txt
COPY . .
USER app
EXPOSE 8800
CMD ["python", "app.py"]
