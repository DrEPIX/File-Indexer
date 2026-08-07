# Container Deployment

This document covers only Codex-owned container packaging. The API command and
health paths below were confirmed against the core owner's CLI contract.

## Current machine note

The development machine has an NVIDIA RTX 4080 (16 GB). Docker CLI is installed,
but Docker Desktop's Linux engine was not running during the initial static
validation on 2026-08-07.

## API container

The image installs `ffmpeg`, `exiftool`, and `libmagic`, then installs the root
Python project. It runs as UID 10001, drops Linux capabilities in Compose, binds
the host port to loopback, mounts originals read-only at `/library`, and stores
mutable state in the `mediaengine-data` volume.

Copy `docker/.env.example` to `docker/.env`, set an absolute library path, and
generate the required 16+ character API bearer token as shown in
`docker/README.md`. Then run from the repository root:

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml config
docker compose --env-file docker/.env -f docker/compose.yaml up --build
```

The entrypoint runs the idempotent migration command and then execs:

```text
mediaengine serve --host 0.0.0.0 --port 8420
```

The command is overrideable through Compose `command:`. The API container probe
uses the intentionally unauthenticated `GET /api/health` endpoint.

## Security boundary

Compose publishes only on `127.0.0.1` by default and still requires a bearer
token because the process binds `0.0.0.0` inside its container. Source libraries
are read-only; the DB, derivatives, and model cache use separate named volumes.

Use `--profile clip` for the CPU analyzer. Add
`-f docker/compose.gpu.yaml` for the RTX/CUDA overlay. Both mount derivatives at
the identical `/data/derivatives` path, read-only in the analyzer, so `paths`
transfer needs no rewriting.

Use `--profile vision` to add face embeddings and object detection. The same
profile runs on CPU; the GPU overlay selects CUDA and reserves an NVIDIA device.
CLIP and vision are separate services so either can be enabled, restarted, or
upgraded without coupling heavyweight model dependencies to the API process.
