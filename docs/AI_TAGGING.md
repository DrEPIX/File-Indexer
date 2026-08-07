# Local AI tagging

MediaEngine runs visual models as isolated analyzer services. They use the
same generic annotation contract as metadata plugins, so every result carries
the asset, namespace, label, confidence, model/version, and optional video
timestamp or region. The core contains no fixed taxonomy and no model can write
directly to the database.

No generative LLM is used. The initial local stack consists of:

| Analyzer | Purpose | Output |
|---|---|---|
| `acme.clip` | Visual embeddings and open-vocabulary category scoring | `clip`, `visual.category` |
| `acme.vision` | COCO object detection and face-region embeddings | `vision.object`, `vision.face` |
| `local.safety` | Dedicated NSFW risk classification | `safety.nsfw`, `safety.nsfw.frame` |

The vision service also accepts any number of operator-supplied Caffe SSD
models through `VISION_CAFFE_MODELS_CONFIG`. Each model has independent files,
labels, preprocessing, threshold, and result limits. Its detections land in a
collision-safe child namespace under `vision.object`; see
`plugins-available/vision-http/caffe-models.example.json`.

For video, analyzers consume generated keyframes. Category scores are blended
across frames; objects retain their bounding box and timestamp; safety uses the
highest-risk sampled frame for the asset rating and stores timestamped review
evidence. This is bounded sampling, not frame-perfect event detection.

## Run the complete stack

CPU:

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml --profile ai up --build
```

NVIDIA GPU:

```powershell
docker compose --env-file docker/.env -f docker/compose.yaml -f docker/compose.gpu.yaml --profile ai up --build
```

After scanning, inspect `GET /api/plugins` and start a backfill for each enabled
analyzer with `POST /api/plugins/{plugin_id}/backfill`. From the CLI:

```powershell
mediaengine plugins list
mediaengine backfill --plugin acme.clip --plugin acme.vision --plugin local.safety
```

Weights download into persistent model volumes on first start. Inference is
local after the cache is populated. An air-gapped installation can pre-populate
those volumes or point each service at a compatible local model directory.

## Category configuration

The starter CLIP taxonomy covers common formats, subjects, activities, and
scenes. It intentionally avoids sensitive personal-trait inference. Replace it
with library-specific prompts without changing code:

```yaml
plugins:
  enabled: [acme.clip, acme.vision, local.safety]
  directories: [./plugins-available]
  allow_network: true
  per_plugin:
    acme.clip:
      prompts: [wildlife, woodworking, lecture, drone footage, product demo]
      tag_threshold: 0.10
      top_k: 5
      max_keyframes: 8
```

Changing per-plugin config changes the producer identity and causes a safe,
attributable re-analysis. User-confirmed annotations continue to outrank model
output.

## NSFW policy and search

The safety model always writes one of `safe`, `review`, or `flagged`. It never
deletes, moves, or automatically hides an original. Frontends choose their own
display policy.

```text
nsfw:safe                       only explicitly rated-safe assets
nsfw:only                       only flagged assets
safety.nsfw:review              review queue
not:safety.nsfw:flagged         exclude flagged assets but retain unrated ones
```

`nsfw:safe` is intentionally strict: unrated media is omitted because the
backend cannot honestly call unprocessed content safe. Default thresholds are
0.45 for review and 0.75 for flagged; tune them on representative local data.

Image-safety classifiers have false positives and false negatives, especially
for animation, medical imagery, art, unusual crops, and domain-shifted footage.
Treat the label as a screening aid, keep a review path, and never use it as the
sole basis for destructive action.
