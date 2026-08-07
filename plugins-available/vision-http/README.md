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

## Caffe detection models

Set `VISION_OBJECT_BACKEND` to `caffe` or `both`. For one legacy model, provide
`VISION_CAFFE_PROTOTXT`, `VISION_CAFFE_MODEL`, and `VISION_CAFFE_LABELS`. For
multiple models, set `VISION_CAFFE_MODELS_CONFIG` to a JSON file based on
`caffe-models.example.json`.

Compose bind-mounts `VISION_CAFFE_MODELS_PATH` read-only at `/models/caffe` and
defaults the config path to `/models/caffe/models.json`. Put your existing model
directories there; do not copy weights into the repository or image.

Each model entry owns its ID, files, labels, input size, scale, mean, channel
swap, confidence threshold, and result cap. Relative file paths resolve from
the JSON file's directory. Labels may be a text-file path or an inline array.
Detections from Caffe models use child namespaces such as
`vision.object.mobilenet-ssd-custom` and include both `backend` and `model_id`
in the annotation value. A parent query such as `vision.object:car` matches all
detectors, while a child namespace selects one model. Separate namespaces also
prevent two models that return the same label and box from overwriting each
other during idempotent commits.

The initial parser supports the widely used Caffe SSD seven-column output
layout (`ssd7`): image id, class id, confidence, and normalized box corners.
Unsupported output layouts fail with a model-specific diagnostic. Invalid
entries and model-load failures are isolated; valid sibling models continue to
run. Weights and prototxt files are operator-owned and never downloaded or
modified by MediaEngine.

The matching `VISION_*` entries in `docker/.env` set installation defaults;
per-plugin request config wins when both are present. These are intentionally
plain variables so changing model sensitivity does not require Python edits.

The first start downloads model weights into the persistent `/models` volume.
After pre-populating the cache, egress can be denied for offline operation.
If `VISION_AUTH_TOKEN` is set for the service, put the same value in
`[plugin.http].auth_token`; otherwise the engine's health and analyze requests
will correctly be rejected.
