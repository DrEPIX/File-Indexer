# Frozen contracts — v1

**Status: FROZEN.** Both agents build against exactly this. Changing anything
here requires a message in `COORDINATION.md` **and an ack** before you touch code.

Protocol identifier used in every payload: `mediaengine.analyzer/1`

There are three ways to be an analyzer. All three emit the *same* annotation
shape; only the transport differs.

| Transport | Runs where | Use when |
|---|---|---|
| `in_process` | inside the engine, imported | cheap, pure-Python, metadata-only |
| `subprocess` | forked child, own venv | heavy deps that conflict with the core |
| `http` | any service, any host, any language | GPU model servers, containers, remote |

---

## 1. The annotation (identical across all transports)

This is the only thing a plugin produces. The core assigns producer ids,
resolves conflicts against user data, and commits. **Plugins never write to the
database.**

```jsonc
{
  "namespace": "garment.color",   // required. dotted, [a-z0-9._-]+, ≤64 chars
  "label": "red",                 // required. non-empty, ≤256 chars
  "value": {"hex": "#c0392b"},    // optional. any JSON object, ≤64 KiB serialized
  "confidence": 0.87,             // optional. float in [0.0, 1.0], or null
  "region": {                     // optional. null = the annotation is asset-level
    "x": 0.12, "y": 0.30,         //   normalized 0..1, origin top-left
    "w": 0.25, "h": 0.40,
    "frame_time": 12.5,           //   seconds into a video, or null for stills
    "page_number": 3,             //   1-based, documents only, or null
    "kind": "face"                //   free-form hint, or null
  },
  "embedding": [0.013, -0.22]     // optional. float32 vector, any dimension
}
```

Rules the core enforces on receipt — violate them and the annotation is
rejected with `PluginContractError`:

- `namespace` and `label` are non-empty after strip.
- `confidence`, if present, is in `[0.0, 1.0]`. Out of range is an error, not a
  clamp — a model emitting 1.4 is a bug you want to see.
- `region` coordinates are normalized, **not pixels**. `w`/`h` ≥ 0.
- Two annotations from the same producer with the same
  `(asset, region, namespace, label)` collapse to one. Emitting duplicates is
  not an error; it is idempotent.
- An `embedding` on an annotation whose `region` is set attaches to that region;
  otherwise it attaches to the asset.

**Namespace conventions** (not enforced, but everything in the UI assumes them):
own your prefix. `acme.*` if you are acme. `core.*` and `example.*` are taken.

---

## 2. The work item (what a plugin receives)

Identical for `subprocess` and `http`. In-process plugins get the same
information through the `AnalysisContext` object instead.

```jsonc
{
  "protocol": "mediaengine.analyzer/1",
  "request_id": "0f3c…",          // echo this back verbatim
  "deadline_s": 120.0,            // wall clock budget; exceed it and you're killed

  "asset": {
    "asset_id": 12345,
    "content_hash": "b3:9f2c…",
    "media_type": "image",        // image|video|audio|document|other
    "mime_type": "image/jpeg",
    "size_bytes": 4194304,
    "captured_at": "2026-04-07T14:22:11.000Z",   // may be null
    "filename": "IMG_4821.HEIC"
  },

  "metadata": {                   // promoted technical columns; keys may be absent
    "width": 4032, "height": 3024, "duration_s": null, "frame_rate": null,
    "orientation": 6, "camera_make": "Apple", "camera_model": "iPhone 14 Pro",
    "iso": 64, "f_number": 1.78, "focal_length": 6.86, "page_count": null
  },

  "derivatives": {                // see §2.1 for how bytes are delivered
    "original":   {"path": "/library/2026/IMG_4821.HEIC"},
    "thumbnails": {"256": {"path": "…/thumb_256.webp"},
                   "512": {"path": "…/thumb_512.webp"}},
    "keyframes":  [{"time": 0.0,  "path": "…/kf_000000.webp"},
                   {"time": 5.0,  "path": "…/kf_000500.webp"}],
    "audio":      {"path": "…/audio.wav"},
    "proxy":      {"path": "…/proxy_720.mp4"}
  },

  "text": "extracted document text or null",

  "prior_annotations": [          // output of plugins this one depends_on
    {"namespace": "core.face", "label": "face",
     "confidence": 0.99, "region": {"x":0.1,"y":0.2,"w":0.1,"h":0.1,"kind":"face"},
     "producer": "core.face-detector@1.0.0"}
  ],

  "config": {}                    // this plugin's slice of plugins.per_plugin
}
```

### 2.1 How bytes reach the plugin — `transfer`

Declared in the manifest. The host honours whatever the plugin asks for.

- **`"paths"`** (default) — every `path` is an absolute path the *plugin* can
  open. Requires a shared filesystem. For Docker, bind-mount the derivatives
  directory at **the same absolute path inside the container** so paths do not
  need rewriting. Cheapest option; use it for local containers.

- **`"inline"`** — the host omits `path` and sends `content_b64` (base64 of the
  file bytes) plus `content_type`. Works across a network with no shared disk.
  The host only inlines derivatives the plugin's `requires` block says it needs,
  because inlining a 4K original into JSON is expensive.

  ```jsonc
  "thumbnails": {"512": {"content_b64": "UklGRi…", "content_type": "image/webp"}}
  ```

- **`"both"`** — the host sends `path` when it can and falls back to
  `content_b64` otherwise. Plugin must handle either. Prefer `path` if present.

Only what `requires` asks for is populated. A `metadata_only` plugin gets
`derivatives: {}` and pays no decode cost. This is the point of the capability
block — **declare the minimum you need**, or you slow down the whole library.

---

## 3. HTTP transport — task B builds against this

The engine is the **client**. Your service never calls the engine. Three
endpoints, all JSON, all `Content-Type: application/json`.

If `auth_token` is configured, the engine sends `Authorization: Bearer <token>`
on every request. Your service should reject requests without it.

### `GET /manifest`

Returns the manifest (§5) as JSON. Called once at registration and again
whenever the engine restarts. **`version` here is authoritative** — bump it and
the engine automatically invalidates prior tasks and re-analyzes the library.

```json
{
  "protocol": "mediaengine.analyzer/1",
  "id": "acme.clip",
  "version": "1.0.0",
  "model_id": "ViT-B-32/laion2b_s34b_b79k",
  "accepts": ["image", "video"],
  "emits": ["clip"],
  "depends_on": [],
  "transfer": "paths",
  "embedding_dim": 512,
  "requires": {
    "pixels": true, "frames": false, "audio": false, "text": false,
    "metadata_only": false, "gpu": true, "network": false, "max_concurrency": 2
  },
  "namespaces": {
    "clip": {"display_name": "Visual similarity", "value_type": "text",
             "facetable": false}
  }
}
```

### `GET /health`

```json
{"status": "ok", "model_loaded": true, "detail": "ViT-B-32 on cuda:0"}
```

`status` is `"ok"` | `"loading"` | `"error"`. The engine treats `loading` as
transient and backs off; it will not send work until `ok`. Return **200 for
`ok` and `loading`**, 503 for `error`. Docker `HEALTHCHECK` should hit this.

### `POST /analyze`

Body is the work item (§2). Success is **200** with:

```json
{
  "protocol": "mediaengine.analyzer/1",
  "request_id": "0f3c…",
  "annotations": [ /* §1 */ ],
  "model_id": "ViT-B-32/laion2b_s34b_b79k",
  "duration_ms": 143
}
```

`annotations: []` is a valid, successful result meaning "nothing to say about
this asset". It is **not** an error and the task is marked done.

Failure is a non-2xx with:

```json
{"error": {"kind": "unsupported_media", "message": "…", "retryable": false}}
```

Status code determines the engine's retry policy, and this is the part to get
right:

| Status | Engine behaviour |
|---|---|
| 200 | success |
| 400, 415, 422 | **permanent** — task `failed`, never retried, logged to `errors` |
| 408, 429, 502, 503, 504 | **transient** — exponential backoff, up to `workers.max_retries` |
| 500 | transient *unless* body has `"retryable": false` |
| connection refused / timeout | transient |

Honour `Retry-After` on 429/503 if you set it; the engine reads it.

---

## 4. Subprocess transport — task C builds against this

JSON Lines over stdin/stdout. One complete JSON object per line, `\n`-terminated.

**stdout is the protocol channel and carries nothing else.** A stray `print()`
corrupts the stream and the host will kill the process. Log to **stderr**, which
the host captures into the engine log. Run Python with `-u` or the buffering
will bite you.

Handshake, then a request/response loop:

```
host → {"type":"hello","protocol":"mediaengine.analyzer/1","config":{…}}
plug → {"type":"ready","manifest":{…§5…}}

host → {"type":"analyze","request_id":"abc",…work item §2…}
plug → {"type":"result","request_id":"abc","annotations":[…]}
   or → {"type":"error","request_id":"abc","kind":"corrupt_media",
         "message":"truncated JPEG","retryable":false}

     (at any time, optional)
plug → {"type":"log","level":"info","message":"loaded model in 4.1s"}
plug → {"type":"progress","request_id":"abc","fraction":0.5}

host → {"type":"shutdown"}
plug → exits 0 within 5s, else SIGTERM, then SIGKILL after 5 more
```

- The host sends one `analyze` at a time per process and waits for the matching
  `request_id`. Concurrency comes from running N processes, set by
  `max_concurrency`.
- A crash is isolated: the host restarts the process and retries the task with
  backoff, then marks it `failed`.
- `retryable` defaults to `false` when absent. Say so explicitly.

---

## 5. `plugin.toml` manifest

Dropped in any directory listed in `plugins.directories`. The engine scans for
`plugin.toml` recursively, one level of subdirectories deep.

```toml
[plugin]
id          = "acme.clip"          # required. stable forever. [a-z0-9._-]+
version     = "1.0.0"              # required. bump ⇒ automatic re-analysis
transport   = "http"               # in_process | subprocess | http
accepts     = ["image", "video"]   # required. subset of image/video/audio/document/other
emits       = ["clip"]             # namespaces, for UI registration
depends_on  = []                   # plugin ids that must finish first, per asset
model_id    = "ViT-B-32"           # optional. part of the producer identity
description = "CLIP image embeddings for semantic search"
transfer    = "paths"              # paths | inline | both
embedding_dim = 512                # optional; see §5.1

[plugin.requires]                  # the capability block. declare the MINIMUM.
pixels          = true
frames          = false
audio           = false
text            = false
metadata_only   = false
gpu             = true
network         = false            # denied unless operator sets plugins.allow_network
max_concurrency = 2                # GPU plugins should say 1 unless batching

# transport = "subprocess"
[plugin.subprocess]
command = ["python", "-u", "run.py"]
cwd     = "."                      # relative to the plugin.toml
env     = { OMP_NUM_THREADS = "1" }

# transport = "http"
[plugin.http]
base_url      = "http://127.0.0.1:9100"
auth_token    = ""                 # optional; sent as Bearer
timeout_s     = 120.0
health_path   = "/health"
manifest_path = "/manifest"
analyze_path  = "/analyze"

# optional self-registration so the UI can render nice filter controls
[plugin.namespaces.clip]
display_name  = "Visual similarity"
value_type    = "text"             # categorical | numeric | text | geo | boolean
facetable     = false
embedding_dim = 512                # optional per-namespace override
```

### 5.1 `embedding_dim` — added in v1.1 (additive, backward compatible)

Optional. Declares the dimension of vectors this plugin emits, so the core can
reject a mismatched vector at commit time instead of silently poisoning the
similarity index.

- **Top-level `embedding_dim`** applies to every embedding the plugin emits.
- **Per-namespace `embedding_dim`** overrides it, for the uncommon case of one
  plugin emitting several embedding kinds at different dimensions (e.g. a
  512-dim image vector and a 128-dim face vector).
- **Absent means unvalidated.** The core still stores whatever it receives and
  reads the true length from the vector itself; `embeddings.dim` is always
  derived from the payload, never from this field. This is a declaration for
  validation and UI, not the source of truth.

When present and a committed vector's length disagrees, the core raises
`PluginContractError`, fails that task, and writes to `errors` — it does not
pad, truncate, or silently accept. A dimension change without a `version` bump
is a bug worth surfacing loudly, because the old and new vectors are not
comparable and cosine similarity across them is meaningless.

Vectors of different dimensions coexist fine in the `embeddings` table; the
search layer only ever compares vectors sharing a `producer_id`.

`in_process` plugins are normally registered through the
`mediaengine.analyzers` entry-point group instead of a manifest.

---

## 6. Producer identity — why version bumps matter

The engine records every derived row against a **producer**, which is the tuple:

```
(plugin_id, version, model_id, config_hash)
```

Change any component and it is a different producer. Consequences you should
design around:

- **Bumping `version` re-analyzes the whole library for that plugin.** Old
  annotations survive until the new ones commit, then get marked
  `superseded_by`. Don't bump for a comment change.
- **Changing a plugin's config re-analyzes it too**, because `config_hash`
  changes. That is intended: output must be attributable to the exact settings
  that produced it.
- `DELETE /api/producers/{id}/annotations` purges everything one producer ever
  wrote, in one transaction. Every plugin must tolerate its output vanishing.

---

## 7. Two rules that override everything else

1. **User data outranks machine data.** If a human confirmed a value, no plugin
   output can supersede it, at any confidence, ever. The core enforces this; you
   don't have to, but don't design around expecting to win.

2. **Network is denied by default.** `requires.network = true` is a *request*.
   It is refused unless the operator sets `plugins.allow_network: true`, and the
   grant is written to `capability_grants`. A plugin that quietly needs the
   internet will simply fail on a locked-down install — declare it.
