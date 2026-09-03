FROM python:3.13-slim-bookworm

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/data/cache

WORKDIR /opt/wa-synergy
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8099
CMD ["python", "-m", "wa_synergy.service", "--options", "/data/options.json"]
