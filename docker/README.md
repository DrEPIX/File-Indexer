# MediaEngine Containers

The API image deploys the engine's HTTP service; it does not replace the plain
pip-installable core used by the native Tk desktop GUI. CLIP category/embedding, object/face,
and safety models run as isolated local HTTP analyzers with independent model
caches and dependency stacks.

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

Use `--profile ai` to start the complete local vision stack (CLIP categories,
objects/faces, and NSFW safety ratings), or `--profile safety` for only the
safety classifier. None of these services uses a generative LLM.

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
detection, or enable all services together. Face embeddings are stored through
the normal producer-aware pipeline and feed identity clustering; the model never
overwrites user-confirmed identity data.

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml -f docker/compose.gpu.yaml --profile ai up --build
```

This requires Docker Desktop with NVIDIA container support. The development
machine has an RTX 4080 16 GB, but its Docker Desktop engine was stopped during
authoring, so static Compose validation is available before live build testing.

## Mount and state boundaries

- `/library` is a read-only bind mount. Originals cannot be modified by either
  container. The API and model containers see it at the same absolute path, so
  an HTTP analyzer can open path-mode originals without path rewriting.
- `/data/db` and `/data/derivatives` are separate named volumes.
- `/data/derivatives` is mounted read-only at the identical absolute path in
  both model containers, satisfying the `transfer = "both"` contract.
- `/models` is a persistent, replaceable per-service model cache.
- `/plugins` is a read-only mount of `plugins-available/` for discovery.

`CLIP_AUTH_TOKEN`, `VISION_AUTH_TOKEN`, and `SAFETY_AUTH_TOKEN` are passed both
to their respective services and to the engine's remote endpoint overrides.
No manifest edit is needed. Blank values keep the internal Compose services
unauthenticated.

Safety output is advisory. The analyzer records `safe`, `review`, or `flagged`
under `safety.nsfw`; it never deletes or moves originals. Search with
`nsfw:safe`, `nsfw:only`, or the generic `safety.nsfw:review` filter.

`plugin-base` is a build-only Compose service scaled to zero replicas. It lets
the analyzer Dockerfiles inherit the shared CPU or CUDA base while keeping
`docker compose up` from starting a useless helper container.

API liveness is `GET /api/health`; each analyzer uses `GET /health`. The API
entrypoint waits for a writable DB directory, runs idempotent migrations, then
execs the server so signals reach PID 1 through `tini`.
