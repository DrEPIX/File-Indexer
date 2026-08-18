# Local AI tagging

MediaEngine runs visual models as isolated analyzer services. They use the
same generic annotation contract as metadata plugins, so every result carries
the asset, namespace, label, confidence, model/version, and optional video
timestamp or region. The core contains no fixed taxonomy and no model can write
directly to the database.

Two independent local stacks are available. The containerised classifier stack
below is discriminative — no generative model — and is the right choice for
large libraries. The `local.lm-studio` analyzer described in
[Local vision models via LM Studio](#local-vision-models-via-lm-studio) is
generative, runs against a model you pick from Studio's Model Store, and is the
right choice when you want written descriptions rather than fixed labels.

The initial local classifier stack consists of:

| Analyzer | Purpose | Output |
|---|---|---|
| `acme.clip` | Visual embeddings, category and genre scoring | `clip`, `visual.category`, `visual.genre` |
| `acme.vision` | COCO object detection and face-region embeddings | `vision.object`, `vision.face` |
| `local.safety` | Dedicated NSFW risk classification | `safety.nsfw`, `safety.nsfw.frame` |
| `core.visual-signals` | Fast exposure, detail, palette and aspect tagging | `visual.*` |

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

The starter CLIP taxonomy covers common formats, subjects, activities, scenes,
camera styles, film/program genres, memes, advertisements, and home-video
styles. Genre suggestions are stored separately under `visual.genre`. Scores
are normalized within each facet so adding a
scene does not dilute an activity score. It intentionally avoids sensitive
personal-trait inference. Replace it
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
      per_group_top_k: 2
      max_keyframes: 8
```

Changing per-plugin config changes the producer identity and causes a safe,
attributable re-analysis. User-confirmed annotations continue to outrank model
output.

For opt-in public-figure matching against reference images you have the right
to process, see [FACE_REFERENCE_PACKS.md](FACE_REFERENCE_PACKS.md). Reference
matches always enter a human review queue and never assign names automatically.
For unnamed local face groups, explicitly linked social profiles, and sourced
Wikipedia biographies, see [PEOPLE_AND_PROFILES.md](PEOPLE_AND_PROFILES.md).

## Local vision models via LM Studio

`local.lm-studio` talks to LM Studio's OpenAI-compatible server on loopback and
emits `llm.summary`, `llm.category`, and `llm.tag`. Like every analyzer it
returns annotations and never writes to the database itself.

### It needs a model that can see

The single most common way this integration appears to "do nothing" is running
it against a text-only model. The analyzer then has nothing but the filename to
describe, so a holiday video becomes "video file, captured 2019". Studio's
**AI Model Store** (`Ctrl+M`) exists to make that state visible and fixable:

- Installed models are listed with a **SEES VIDEO** or **TEXT ONLY** badge,
  read from LM Studio's own metadata rather than guessed from the name.
- Vision-capable models that are *not* installed are listed with their download
  size and rough VRAM requirement. **Install** hands the download to
  `lms get --yes` and streams its progress into the card; partial downloads
  resume, and cancelling keeps what was already fetched.
- Any model name or Hugging Face URL that `lms get` accepts can be typed into
  the search field and installed directly.
- **Use for tagging** points the analyzer at that model, enables it, loads it
  into memory, and offers to start categorizing videos immediately.

Installing models requires LM Studio's CLI. If `lms` is missing the store says
so and still lists whatever you installed by hand.

### Video is sent as keyframes

For video the analyzer samples frames evenly across the whole clip — not from
the front, where recordings tend to open on a title card or a black frame — and
sends them as one multi-image request with an instruction to describe the clip
as a whole. Frames come from derivatives the scan already produced, so enabling
this costs inference time and no re-decoding. `frame_count` controls how many
(1–8, default 4).

```yaml
plugins:
  enabled: [local.lm-studio]
  allow_network: true          # loopback still counts as network
  per_plugin:
    local.lm-studio:
      base_url: http://127.0.0.1:1234
      model: qwen3-vl-8b        # must be a model the server actually serves
      send_image: true          # default; false makes it metadata-only
      frame_count: 4
      frame_pixels: 512
```

A `model` the server is not serving now fails with the list of ids that would
have worked, instead of returning HTTP 400 on every asset in the library.

### Reasoning models

Models that think out loud — Qwen3 with thinking on, DeepSeek-R1, gpt-oss —
return an empty `content` and put everything, including the final JSON, in
`reasoning_content`. The analyzer reads either, and strips inline `<think>`
blocks, so a reasoning model works rather than failing on every asset. If one
spends its whole token budget before answering, the error says so and names the
fix instead of reporting an empty reply.

### Free-text tags vs. fixed filters

`local.lm-studio` writes open-ended `llm.tag` values: whatever the model
considered worth saying. That is good for recall and bad for filtering, because
the same idea comes back spelled three ways.

When you want a *filter* — a fixed set of values you can click — use a filter
pack instead. Packs constrain the model to a closed vocabulary with a JSON
schema, so the facet cannot fill up with synonyms. See
[FILTER_PACKS.md](FILTER_PACKS.md). The two are complementary: tags describe,
packs sort.

### Scope the run

A generative analyzer accepts every media type, so an unscoped backfill queues
one inference call per file. The Analyzer Store's run control scopes a sweep to
videos, photos, documents, or everything, and shows the queue as
`done / failed / pending` so a long run is legible rather than mysterious.
Failures caused by a stopped model server are recoverable with **Retry failed**
— there is no need to rebuild the library. The same controls exist in code:

```python
engine.backfill(["local.lm-studio"], media_types=["video"], retry_failed=True)
```

Because `send_image` changes what the model was shown, upgrading the analyzer
supersedes the claims made by the previous version rather than sitting beside
them. Filename-only tags from an earlier run drop out of live views once the
new version re-analyses that asset, and the audit trail is preserved.

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
