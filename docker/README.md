# MediaEngine Containers

The API image deploys the engine's HTTP service; it does not replace the plain
pip-installable core used by the PyQt GUI. The CLIP service is an isolated HTTP
analyzer with its own dependency stack and model cache.

## Prepare

Copy `.env.example` to `.env`, point `MEDIAENGINE_LIBRARY_PATH` at a host media
directory, and generate the mandatory API token:

```powershell
Copy-Item docker/.env.example docker/.env
$token = -join ((1..48) | ForEach-Object { 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'[(Get-Random -Maximum 62)] })
(Get-Content docker/.env) -replace '^MEDIAENGINE_AUTH_TOKEN=.*$', "MEDIAENGINE_AUTH_TOKEN=$token" | Set-Content docker/.env
```

The API binds `0.0.0.0` *inside* its container, so MediaEngine correctly refuses
to start without a token. Compose publishes it only as
`127.0.0.1:8420:8420`; it is not exposed to the LAN by default.

## CPU stack

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml up --build
```

Add `--profile clip` to build and run the CPU CLIP analyzer. Model weights are
downloaded at runtime into the `clip-models` volume, never baked into the image.
The Compose stack explicitly grants plugin network access because the first
uncached CLIP start downloads weights. After the cache is populated, operators
who require an offline installation can set
`MEDIAENGINE__PLUGINS__ALLOW_NETWORK=false` and deny container egress.

## NVIDIA GPU stack

The GPU overlay uses a CUDA 12 runtime, official PyTorch cu121 wheels, and one
reserved NVIDIA GPU per analyzer. CUDA is optional: the same services run in
the CPU profiles and still fall back safely outside the containers. The base
stack denies plugins that *require* a GPU; applying the overlay explicitly
grants that capability.

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml -f docker/compose.gpu.yaml --profile clip up --build
```

Use `--profile vision` for face learning/recognition vectors and COCO object
detection, or enable both profiles together. Face embeddings are stored through
the normal producer-aware pipeline and feed identity clustering; the model never
overwrites user-confirmed identity data.

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml -f docker/compose.gpu.yaml --profile clip --profile vision up --build
```

This requires Docker Desktop with NVIDIA container support. The development
machine has an RTX 4080 16 GB, but its Docker Desktop engine was stopped during
authoring, so static Compose validation is available before live build testing.

## Mount and state boundaries

- `/library` is a read-only bind mount. Originals cannot be modified by either
  container.
- `/data/db` and `/data/derivatives` are separate named volumes.
- `/data/derivatives` is mounted read-only at the identical absolute path in the
  CLIP container, satisfying the `transfer = "paths"` contract.
- `/models` is the persistent, replaceable CLIP model cache.
- `/plugins` is a read-only mount of `plugins-available/` for discovery.

`plugin-base` is a build-only Compose service scaled to zero replicas. It lets
the CLIP Dockerfile literally inherit the shared CPU or CUDA base while keeping
`docker compose up` from starting a useless helper container.

API liveness is `GET /api/health`; CLIP liveness is `GET /health`. The API
entrypoint waits for a writable DB directory, runs idempotent migrations, then
execs the server so signals reach PID 1 through `tini`.
