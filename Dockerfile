FROM python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements/base.txt requirements/base.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements/base.txt

COPY --chown=app:app . .

RUN mkdir -p /var/lib/pmdap/identity /var/lib/pmdap/medical && \
    chown app:app /var/lib/pmdap/identity /var/lib/pmdap/medical

USER app

EXPOSE 8000

CMD ["sh", "docker/entrypoint.sh"]

FROM runtime-base AS web

FROM runtime-base AS ocr-worker

USER root

RUN python -m pip install -r requirements/ocr.txt

RUN apt-get update && \
    apt-get install --yes --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

ENV PADDLE_PDX_CACHE_HOME=/opt/paddle-cache \
    PADDLE_PDX_MODEL_SOURCE=BOS

RUN mkdir -p /opt/paddle-cache && \
    python docker/preload_ocr_models.py && \
    chown -R app:app /opt/paddle-cache

ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1

USER app
