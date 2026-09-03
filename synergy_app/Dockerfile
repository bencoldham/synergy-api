ARG BUILD_FROM=python:3.13-slim-bookworm
FROM ${BUILD_FROM}

ARG BUILD_REF=main
ENV PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/data/cache

RUN python -m pip install --no-cache-dir \
      "https://github.com/bencoldham/synergy-api/archive/${BUILD_REF}.tar.gz" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8099
CMD ["python", "-m", "wa_synergy.service", "--options", "/data/options.json"]
