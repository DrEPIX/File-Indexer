# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12
ARG CUDA_VERSION=12.4.1

FROM python:${PYTHON_VERSION}-slim AS cpu
ARG PLUGIN_UID=1000
ARG PLUGIN_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl libgl1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${PLUGIN_GID}" analyzer \
    && useradd --uid "${PLUGIN_UID}" --gid "${PLUGIN_GID}" \
        --create-home --shell /usr/sbin/nologin analyzer \
    && mkdir -p /models /work \
    && chown -R analyzer:analyzer /models /work
USER analyzer
WORKDIR /work

FROM nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu22.04 AS cuda
ARG PLUGIN_UID=1000
ARG PLUGIN_GID=1000
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl libgl1 libglib2.0-0 python3 python3-pip tini \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && groupadd --gid "${PLUGIN_GID}" analyzer \
    && useradd --uid "${PLUGIN_UID}" --gid "${PLUGIN_GID}" \
        --create-home --shell /usr/sbin/nologin analyzer \
    && mkdir -p /models /work \
    && chown -R analyzer:analyzer /models /work
USER analyzer
WORKDIR /work

