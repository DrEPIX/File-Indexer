# Codex prompt — MediaEngine parallel work

> Charlie: paste everything below the line into Codex. It is self-contained.

---

You are working on **MediaEngine**, a local-first, plugin-driven media indexing
engine in Python 3.11+. Another agent (Claude, running in a cloud sandbox) is
building the engine core in parallel and delivering it to
`F:\File Indexer\mediaengine\`. You are on the local machine, so you have the
real filesystem and a working Docker daemon — Claude has neither of those for
your machine, which is exactly why these tasks are yours.

## Read these first, in this order

1. `F:\File Indexer\_coordination\CONTRACTS.md` — **frozen wire contracts.**
   This is the whole basis for us working in parallel. Build against it exactly.
   If something in it is wrong or impossible, post a message in
   `COORDINATION.md` and wait for an ack before deviating.
2. `F:\File Indexer\_coordination\FILE_OWNERSHIP.md` — who owns which paths.
3. `F:\File Indexer\_coordination\COORDINATION.md` — the shared message log.
   **Append your reply there first**, saying which tasks you've claimed, before
   writing any code.
4. `F:\File Indexer\media-engine-prompt.md` — the original full spec, for
   context on the design principles. You are not implementing the core.

## Hard constraints

- **Do not create or edit anything under `mediaengine/`, `tests/`, or
  `pyproject.toml`.** Claude owns those. Two agents writing one file across two
  filesystems loses work silently — there is no merge here.
- Your files go in `docker/`, `examples/`, `plugins-available/`, `scripts/`.
- Everything you build must run with the core **absent**. Claude's code may not
  have landed yet. A plugin service is a standalone process that speaks JSON; it
  must be testable with `curl` alone.
- Python 3.11+, full type annotations, `mypy --strict` clean.
- No stubs, no `TODO`s in anything you declare finished.

---

# Task A — Docker for the API service

**Goal:** a container that runs the engine's HTTP API, plus a compose stack that
brings up the API and the CLIP analyzer together.

Design decision already made, don't revisit: **the core stays a plain
pip-installable package.** Charlie is building a PyQt GUI that embeds the engine
in-process; a container would force an HTTP hop for the GUI's own data. Docker
here is for *deployment of the API service* and for *sandboxing heavy plugins*,
not for the engine itself.

Build:

```
docker/
  api.Dockerfile          multi-stage; runtime stage has ffmpeg + exiftool
  plugin-base.Dockerfile  shared base for HTTP analyzers (see task B)
  compose.yaml            api + clip services, named volumes, healthchecks
  compose.gpu.yaml        overlay adding NVIDIA device reservations
  entrypoint-api.sh       waits for the DB dir, runs migrations, execs uvicorn
  README.md               operator docs: what to mount, what to set, how to run
```

Requirements:

- Runtime image installs `ffmpeg` and `libimage-exiftool-perl`. The engine
  shells out to `ffprobe`, `ffmpeg` and `exiftool`; without them extraction
  degrades badly.
- **Run as a non-root user.** Create `mediaengine` uid 1000. Make the uid/gid
  build args so Charlie can match his host ownership on bind mounts.
- **Mount library roots read-only** (`:ro`). Principle 1 of the spec is "never
  modify originals" — enforce it at the mount, not just in code.
- Derivatives and the DB go in named volumes, or a bind mount under
  `./data`. Never inside a library root.
- The API binds `127.0.0.1` by default. Inside a container it must bind
  `0.0.0.0` to be reachable, which the config validator **rejects without an
  auth token** — that is deliberate. So: `compose.yaml` must generate or require
  `MEDIAENGINE__API__AUTH_TOKEN` and publish the port as `127.0.0.1:8420:8420`
  so it is not on the LAN by accident. Document this clearly; it will otherwise
  look like a bug to whoever runs it.
- `HEALTHCHECK` hitting `GET /api/health`.
- Config via env vars using the `MEDIAENGINE__` prefix and `__` nesting
  (e.g. `MEDIAENGINE__STORAGE__DB_PATH`). A mounted `config.yaml` also works.
- Keep the final image lean: build wheels in a builder stage, copy them in, no
  compilers in the runtime layer.

Deliverable check: `docker compose up` starts, `/api/health` returns 200, and
`docker compose --profile clip up` also brings up the analyzer from task B.

---

# Task B — Containerized CLIP analyzer (HTTP transport)

**Goal:** a real, working ML analyzer that gives the engine's vector search
something to actually search. This is the proof that the HTTP plugin contract
works with a heavyweight model.

Build:

```
plugins-available/clip-http/
  plugin.toml         manifest, transport = "http"   (schema in CONTRACTS.md §5)
  server.py           FastAPI service: /manifest /health /analyze
  model.py            model load + encode, isolated from the HTTP layer
  requirements.txt
  Dockerfile          FROM the plugin-base image
  README.md           what it emits, how to point the engine at it
  test_contract.py    contract tests that run WITHOUT the engine
```

Behaviour:

- Model: `open_clip` with `ViT-B-32` / `laion2b_s34b_b79k`, or
  `transformers` `openai/clip-vit-base-patch32`. Your call — pick whichever
  installs cleanly on Windows+Docker and say which in the README.
- **Do not bake weights into the image.** Download at runtime into
  `/models`, which is a mounted volume. Set `HF_HOME` / `TORCH_HOME` there. A
  2GB image you must rebuild to swap models is the wrong shape.
- Load the model **lazily on first use**, and report
  `{"status": "loading"}` from `/health` until it's ready. The engine backs off
  on `loading` rather than failing tasks — this is why that state exists.
- `POST /analyze` emits, per asset:
  - one annotation `namespace="clip"`, `label="embedding"`, with `embedding`
    set to the raw image vector. **Emit whatever dimension your model produces**
    — 512 for ViT-B-32. Do not pad or truncate; the core reads `dim` from the
    payload.
  - optionally, zero-shot labels in namespace `clip.tag` with real confidences,
    from a prompt list configurable via `config`. Keep the default list short
    and generic (indoor/outdoor/document/screenshot/portrait/landscape). This
    exists to prove facets populate, not to be a good classifier.
- For `media_type == "video"`, embed up to N keyframes and emit one annotation
  per keyframe with `region.frame_time` set, plus one mean-pooled asset-level
  embedding. N configurable, default 5.
- Honour `deadline_s`. Return 503 with `Retry-After` if the model is still
  loading. Return 400 for media you cannot decode. Get the status codes right —
  CONTRACTS.md §3 has the table, and they drive the engine's retry policy.
- Support `transfer = "paths"` at minimum. Add `"inline"` (base64) too if it's
  cheap; that's what makes the analyzer work on a different host from the engine.
- Batch within a request if multiple keyframes are present. One forward pass for
  five frames, not five.
- CPU must work. **Ask Charlie whether he has an NVIDIA GPU** before deciding
  whether to pull CUDA wheels — that's a 3GB difference. Note the answer in
  `COORDINATION.md`.

`test_contract.py` must verify, with the engine absent:

- `/manifest` matches the schema in CONTRACTS.md §5,
- `/health` reports `loading` then `ok`,
- `/analyze` on a small fixture JPEG returns a well-formed annotation whose
  `embedding` length equals the manifest's declared dim,
- a corrupt image yields 400 with `retryable: false`,
- an unknown `media_type` yields an empty `annotations` list and 200, not an error.

---

# Task C — Example subprocess (JSONL) plugin

**Goal:** the copy-paste template a plugin author starts from. It is
documentation that happens to execute.

Build:

```
examples/subprocess-plugin/
  plugin.toml
  run.py            complete, correct, ~150 lines, heavily commented
  README.md         how to register it, how to test it by hand
  test_harness.py   drives run.py over a pipe, no engine needed
```

`run.py` emits two hardcoded annotations in namespace `example.subprocess` and
demonstrates the whole protocol correctly:

- reads JSONL from stdin, writes JSONL to stdout, **logs only to stderr**,
- `-u` / explicit flush after every write,
- handles `hello` → `ready`, `analyze` → `result`, `shutdown` → clean exit 0,
- echoes `request_id` verbatim,
- shows a deliberate `error` response path with `retryable` set,
- shows reading a derivative from `derivatives.thumbnails["512"].path`,
- shows consuming `prior_annotations`,
- fails loudly on a malformed line rather than silently continuing.

The comments matter more than the code. Someone should be able to write a
conforming plugin from this file plus CONTRACTS.md without reading the core.

`test_harness.py` spawns `run.py`, runs the full handshake, sends a synthetic
work item, asserts the response shape, sends `shutdown`, asserts exit 0 within
5 seconds. No pytest dependency on the engine.

---

# When you finish each task

Append to `F:\File Indexer\_coordination\COORDINATION.md`:

```
### [HH:MM] Codex → Claude
Finished task X. Files: …
Deviations from CONTRACTS.md: none | <describe>
Blocked on: <anything you need from the core>
```

If you hit something in the contracts that is genuinely wrong — not merely
inconvenient — say so there and propose the fix rather than working around it
silently. A silent workaround becomes an integration failure later, and the
whole point of freezing the contract was to avoid that.
