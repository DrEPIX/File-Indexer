# Container Deployment

This document covers only Codex-owned container packaging. Core CLI/API behavior
is owned by Claude and may supersede the provisional startup command below.

## Current machine note

The development machine has an NVIDIA RTX 4080 (16 GB). Docker CLI is installed,
but Docker Desktop's Linux engine was not running during the initial static
validation on 2026-08-07.

## API container

The image installs `ffmpeg`, `exiftool`, and `libmagic`, then installs the root
Python project. It runs as UID 10001, drops Linux capabilities in Compose, binds
the host port to loopback, mounts originals read-only at `/library`, and stores
mutable state in the `mediaengine-data` volume.

Copy `docker/.env.example` to `docker/.env`, set an absolute library path if
desired, then run from the repository root:

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml config
docker compose --env-file docker/.env -f docker/compose.yaml up --build
```

The image currently defaults to:

```text
python -m mediaengine serve --config /config/config.yaml --host 0.0.0.0 --port 8420
```

That command is intentionally overrideable via Compose `command:`. It must be
confirmed against Claude's delivered CLI before release. The container probe
defaults to `GET /health`; set `MEDIAENGINE_HEALTH_PATH` if the core exposes a
different unauthenticated liveness endpoint.

## Security boundary

Compose publishes only on `127.0.0.1` by default. Do not broaden that binding
unless the core has a configured bearer token. Source libraries are read-only;
database, logs, derivatives, model caches, and other mutable state must use
separate volumes.

