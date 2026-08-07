# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS wheel-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . /src
RUN python -m pip wheel --wheel-dir /wheels ".[hash,detect,documents,api,remote,vec]"

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG MEDIAENGINE_UID=1000
ARG MEDIAENGINE_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        curl \
        ffmpeg \
        libimage-exiftool-perl \
        libmagic1 \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${MEDIAENGINE_GID}" mediaengine \
    && useradd --uid "${MEDIAENGINE_UID}" --gid "${MEDIAENGINE_GID}" \
        --create-home --shell /usr/sbin/nologin mediaengine \
    && mkdir -p /config /data/db /data/derivatives /library /plugins \
    && chown -R mediaengine:mediaengine /data

COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY docker/entrypoint-api.sh /usr/local/bin/mediaengine-entrypoint
RUN chmod 0555 /usr/local/bin/mediaengine-entrypoint

USER mediaengine
WORKDIR /data
EXPOSE 8420
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl --fail --silent http://127.0.0.1:8420/api/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mediaengine-entrypoint"]
