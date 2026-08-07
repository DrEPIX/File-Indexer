# Face and object vision analyzer

This optional HTTP analyzer uses the same frozen plugin protocol as CLIP. It
detects faces, emits normalized 512-dimensional VGGFace2 embeddings, and detects
COCO objects. MediaEngine's identity layer can cluster the face vectors and
associate clusters with user-confirmed people; the analyzer does not invent
names or modify user identity data.

CUDA is an acceleration mode, not a requirement. `VISION_DEVICE=auto` selects
CUDA when PyTorch sees a supported NVIDIA device and otherwise uses CPU. Set it
to `cuda` in the GPU Compose overlay or `cpu` to force deterministic fallback.

Run CPU and GPU profiles from the repository root:

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml --profile vision up --build
docker compose --env-file docker/.env -f docker/compose.yaml -f docker/compose.gpu.yaml --profile vision up --build
```

Per-plugin settings are passed through `plugins.per_plugin.acme.vision`:

- `face_threshold` (default `0.90`)
- `object_threshold` (default `0.70`)
- `max_faces` (default `32`)
- `max_objects` (default `100`)
- `max_keyframes` (default `3`)

The matching `VISION_*` entries in `docker/.env` set installation defaults;
per-plugin request config wins when both are present. These are intentionally
plain variables so changing model sensitivity does not require Python edits.

The first start downloads model weights into the persistent `/models` volume.
After pre-populating the cache, egress can be denied for offline operation.
If `VISION_AUTH_TOKEN` is set for the service, put the same value in
`[plugin.http].auth_token`; otherwise the engine's health and analyze requests
will correctly be rejected.
