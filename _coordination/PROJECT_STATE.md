# MediaEngine — project state

Handoff document. Written 2026-08-07 by Claude (Cowork) at the point where work
moved to Claude Code running locally. Read `CLAUDE.md` first for the rules;
this file is the detail.

---

## 1. Where things stand

**Milestone 1 is complete, tested, and on disk.** Nothing else is started.

| Milestone | State | Owner |
|---|---|---|
| 1. Config, DB, migrations, single-writer connection layer | **done** | Claude |
| 2. Walker, identity, extractors, derivatives | not started | Claude |
| 3. Plugin contract, registry, scheduler, 3 transports | not started | Claude |
| 4. Search: FTS + structured filters + facets | not started | Claude |
| 5. Spatial + vector search | not started | Claude |
| 6. Identities / clusters layer | not started | Claude |
| 7. FastAPI + WebSocket + full REST | not started | Claude |
| 8. Out-of-process plugin host | not started | Claude |
| A. Docker: API image + compose | in progress | Codex |
| B. CLIP HTTP analyzer (containerized) | in progress | Codex |
| C. Example subprocess (JSONL) plugin | in progress | Codex |

Codex was asked to pause mid-flight so the first commit wouldn't capture
half-written files. **Before resuming, read the `[pause]` block in
`_coordination/COORDINATION.md` and check whether Codex posted its file
inventory.** If it did not, ask it to before touching anything it owns.

---

## 2. What exists on disk

### Claude's code — `mediaengine/` (21 files, all tested)

```
pyproject.toml                          packaging, entry points, mypy/pytest config
.gitignore  .dockerignore
mediaengine/
  __init__.py         lazy re-exports so `import mediaengine` stays cheap
  types.py            MediaType, AnnotationSource, TaskState, ScanState,
                      FileStatus, RelationKind, LocationSource
  errors.py           exception hierarchy split into TransientError /
                      PermanentError — the retry policy branches on those two
                      base classes and nothing else
  util.py             time, JSON, stable_hash, chunked, coerce_float/int,
                      JsonFormatter, setup_logging
  config.py           Pydantic settings; YAML + MEDIAENGINE__ env overrides
  py.typed
  db/
    __init__.py
    connection.py     Database: pragmas, thread-local readers, migrations,
                      haversine_km + regexp SQL functions, sqlite-vec probe
    writer.py         the single writer thread
    migrations/0001_initial.sql        30 tables/views
    repositories/
      __init__.py     Repositories facade holding all eight
      base.py         _write/_submit dispatch, _insert/_update/_upsert helpers
      assets.py       assets, files, relations, technical_metadata,
                      document_text, triage fast path, duplicate detection,
                      per-asset export
      annotations.py  producers, regions, annotations, namespaces, facets,
                      the commit path, purge — the extension point
      tasks.py        scan_sessions, analysis_tasks queue, errors,
                      plugin_registry, capability_grants, kv
      places.py       asset_locations, places, R*Tree bbox/radius/map clustering
      tags.py         TagRepository, DerivativeRepository, SearchDocRepository
      identities.py   embeddings, identities, clusters, region_identity,
                      purge_biometrics
```

### Codex's code

`docker/` (8 files), `examples/` (6 files), `plugins-available/clip-http/`,
`qol_contract/`, `docs/`, `AGENTS.md`, `AI_COLLABORATION_LOG.md`. Completeness
unknown — Codex was paused mid-work.

### Shared

`_coordination/` — `CONTRACTS.md` (frozen v1.1), `COORDINATION.md` (append-only
message log), `FILE_OWNERSHIP.md`, `CODEX_PROMPT.md`, this file.
`media-engine-prompt.md` — the original full specification.

---

## 3. Schema — what to know before touching it

30 tables and views in `0001_initial.sql`. Timestamps are ISO-8601 UTC strings
ending in `Z`, so string comparison equals chronological comparison.

**Identity.** `assets` is keyed on `content_hash` (UNIQUE). `files` has N rows
per asset — a moved or copied file is the same asset with another path.
`asset_relations` links RAW+JPEG pairs, motion photos and sidecars.

**Annotations — the extension point.** `annotations(asset_id, region_id,
namespace, label, value_json, confidence, source, producer_id, created_at,
superseded_by)`. `regions` carries normalized 0..1 boxes plus optional
`frame_time` (video) and `page_number` (documents). `producers` is the tuple
`(plugin_id, version, model_id, config_hash)` — change any component and it is a
different producer, which is what makes output attributable to exact settings.

**Idempotency is enforced by a unique index:**

```sql
CREATE UNIQUE INDEX idx_ann_unique
  ON annotations(asset_id, COALESCE(region_id, -1), namespace, label, producer_id);
```

`COALESCE` because SQLite treats NULLs as distinct in a UNIQUE constraint, which
would otherwise let a producer insert unlimited duplicate asset-level rows.

**Deliberate deviations from the original spec**, both to be recorded in
`docs/ARCHITECTURE.md`:

1. **FTS5 uses external-content mode over a `search_docs` table**, not
   `content=''` as sketched. Contentless FTS5 cannot `DELETE` by rowid without
   SQLite ≥ 3.43 (`contentless_delete=1`) or replaying the exact original column
   values. Re-indexing one asset after a plugin adds labels is the *common*
   operation here, so external content is the simpler correct choice: storage
   cost is the same order, and delete/update become ordinary SQL. Three triggers
   (`search_docs_ai/ad/au`) keep the index in lockstep, so writing `search_docs`
   is the only thing the indexer must remember to do.
2. **`producers.model_id` and `config_hash` default to `''`, not NULL** — NULLs
   are distinct under UNIQUE, which would mint a fresh producer row on every
   single call.

**Tags are normalized** — a `tags` table plus an `asset_tags` junction, no array
column. `frequency` is the `tag_frequency` **view**, not a column, so it cannot
go stale.

**Spatial** is the R*Tree module (`asset_geo`), storing degenerate boxes since
every asset is a point. SpatiaLite is not assumed. Radius search is bbox
pre-filter → `haversine_km()` post-filter. `in_bbox` splits boxes crossing the
antimeridian, which is otherwise a silent wrong-answer bug.

---

## 4. Concurrency model

SQLite in WAL mode permits one writer and many concurrent readers. Rather than
fight `SQLITE_BUSY` across a dozen worker threads, **every** write is funnelled
through one dedicated thread holding the only read-write connection.

- `writer.submit(fn)` → `Future`; `writer.run(fn)` blocks. `fn` is
  `(sqlite3.Connection) -> T`.
- Transactional jobs are wrapped in `BEGIN IMMEDIATE … COMMIT`. `BEGIN
  IMMEDIATE` takes the write lock up front, turning a mid-transaction
  `SQLITE_BUSY` into an immediate retryable failure.
- `submit_raw`/`run_raw` skip the wrapper, for statements that cannot run inside
  a transaction: `VACUUM`, `executescript`, `PRAGMA wal_checkpoint`.
- The queue is **bounded** — backpressure, so a runaway producer blocks instead
  of growing memory until the OOM killer intervenes.
- Shutdown drains the queue, checkpoints the WAL, then closes.

**Migration gotcha, already handled:** `executescript()` implicitly commits any
open transaction before running, so `BEGIN`/`COMMIT` must live *inside* the
script text. DDL is transactional in SQLite, which is what makes a failed
migration leave no half-built schema.

---

## 5. Two bugs found during milestone 1 — both worth remembering

**Duplicate regions defeated idempotency.** Regions were inserted unconditionally
on every commit. Re-running a producer created a second geometrically identical
region, so the annotation attached to it got a different `region_id` and slipped
past the unique index — silently duplicating everything on every re-analysis.
Fixed by looking up an existing region matching
`(asset_id, producer_id, x, y, w, h, frame_time, page_number, kind)` first.
Embeddings got the same treatment: a re-run replaces the vector rather than
appending a second one, which would double-count in similarity search.

Verified: three consecutive identical commits now yield
`ann=2, regions=1, embeddings=1`.

**`purge_biometrics` evaluated its target set lazily.** The SQL subquery
identifying biometric regions included `id IN (SELECT region_id FROM
region_identity)`, but `region_identity` was deleted partway through the
transaction — so regions that were biometric *only* by virtue of carrying an
identity link survived the purge. This is exactly the failure mode the function
exists to prevent. Fixed by materialising the target ids before any deletion.

The general lesson for the rest of the build: **a subquery that references a
table the same transaction is deleting from is a correctness bug**, not a style
issue.

---

## 6. Verified behaviour

Manually exercised end-to-end against a temp database:

- Migration applies; re-running is a no-op; `PRAGMA integrity_check` returns ok.
- FTS insert/update/delete stays in sync through the triggers; cascade delete of
  an asset removes its `search_docs` row and its FTS terms.
- `haversine_km(SF, NYC)` = 4129.1 km. R*Tree bbox and radius queries return
  correct ids. Map grid clustering aggregates.
- Producer v1 → v2 commit marks 2 annotations superseded, 1 retracted; live view
  shows only v2; **all 4 rows remain** for audit.
- A user annotation blocks a subsequent plugin commit on the same
  `(namespace, label)` — `blocked_by_user: 1`.
- `confirm_region_identity` then a plugin `link_region` raises
  `ImmutableUserDataError`.
- `purge_biometrics` removes an identity-linked region of non-face `kind`, and
  leaves assets and non-face annotations intact.
- Task claim/complete transitions; `counts_by_state` correct.
- `sqlite-vec` loaded successfully in the build container — treat as optional.

**No pytest suite exists yet.** The above was ad-hoc verification. Writing
`tests/` is part of the remaining work and should come with milestone 2.

---

## 7. Next task — milestone 2 in detail

Build `mediaengine/core/`: `identity.py`, `walker.py`, `extractors/`,
`derivatives.py`, `pipeline.py`. Then CLI `scan` and `stat`.

**Stages, each independently resumable:**

1. **Walk** — recursive traversal honouring include/exclude globs,
   `follow_symlinks`, hidden-file policy, `max_depth`, `cross_filesystem`. Emit
   candidate paths in **batches** through a bounded queue. The walker must block
   rather than materialise 200k paths in memory. Record a `scan_session` and
   update its `cursor` so a kill mid-scan resumes.
2. **Triage** — `stat()` each path; compare `(size, mtime_ns, inode)` against
   `files`. Unchanged → mark seen, skip. `AssetRepository.triage_batch()` already
   exists and does one query per batch; **use it**, not per-file queries. This
   path must handle a 200k-file library in seconds.
3. **Identify** — hash content with BLAKE3, falling back to SHA-256. Detect
   media type by **magic bytes** (`filetype`/`python-magic`), never by extension
   alone. Compute a dhash perceptual hash for images. Create or link the asset.
4. **Extract** — dispatch by media type:
   - video/audio: `ffprobe -v quiet -print_format json -show_format
     -show_streams -show_chapters`
   - images: **exiftool in `-stay_open True -@ argfile` daemon mode**. Per-file
     spawns are roughly an order of magnitude slower. Needs health checks and
     restart-on-crash. Pillow for dimensions and a quick decode check. Parse
     EXIF GPS to decimal degrees handling N/S/E/W refs and altitude ref.
   - documents: PyMuPDF, python-docx, openpyxl, plain text, EPUB. A PDF with no
     text layer is marked **OCR-eligible**, not OCR'd inline.
5. **Derive** — thumbnails at `storage.thumbnail_sizes` as webp; video keyframes
   at a fixed interval plus scene-change detection; optional video proxy;
   extracted audio. Content-addressed:
   `derivatives/<hash[0:2]>/<hash>/thumb_512.webp`. **Never write next to
   originals.** Record every derivative via `DerivativeRepository.record()` so
   the cache is purgeable without walking the tree.
6. **Enqueue analysis** — one `analysis_tasks` row per registered analyzer whose
   `accepts` matches. (Deferred to milestone 3, but leave the hook.)
7. **Index** — rebuild the asset's `search_docs` row and geo row.

**Also required at this milestone:** sidecar/RAW/motion-photo handling.
`.xmp` sidecars; RAW formats CR2/CR3, NEF, ARW, DNG, RAF, ORF; iOS/Android
motion photos (paired HEIC+MOV, or an embedded MP4 in a JPEG). Link them with
`AssetRepository.add_relation()`. Convention already set: for a RAW+JPEG pair the
**JPEG is the parent** because that is what renders in a grid.

**Concurrency for this milestone:** bounded pools — filesystem walk (1),
metadata subprocesses (`ProcessPoolExecutor`, default `cpu_count`), derivative
generation (`cpu_count`). Every subprocess gets a hard timeout and is killed
**with its process group** on expiry. Sanitize all args; never `shell=True`.
`asyncio`/`aiofiles` is the wrong tool here — the bottleneck is process spawns
and CPU-bound decode, not file reads.

**Config knobs already defined** and waiting to be honoured: `scan.*`
(hash_algorithm, compute_perceptual_hash, video_keyframe_interval_s,
video_scene_threshold, detect_sidecars, detect_motion_photos, batch_size),
`workers.*` (metadata_processes, derivative_workers, subprocess_timeout_s,
exiftool_daemon, exiftool_batch_size), `storage.*` (thumbnail_sizes,
thumbnail_format, video_proxy).

---

## 8. Plugin contracts — frozen at v1.1

Full text in `_coordination/CONTRACTS.md`. Summary of what the core must
implement in milestone 3:

- **Three transports**, all emitting the same annotation shape: `in_process`
  (imported), `subprocess` (JSONL over stdin/stdout), `http` (any service, any
  language — the engine is always the client).
- **`AnalysisContext`** exposes lazy, cached accessors — `thumbnail(size)`,
  `image()`, `frames(every=)`, `audio_path()`, `text()`, `annotations(namespace)`
  — so cheap plugins never pay decode cost and expensive decodes are shared
  across every plugin running on the same asset.
- **`depends_on`** is topologically sorted; dependencies are guaranteed complete
  for that asset before a dependent runs. This is how a face-*recognition*
  plugin consumes regions from a face-*detection* plugin.
- **`embedding_dim`** (added v1.1, at Codex's request): optional manifest field.
  Absent means unvalidated. When present and a committed vector disagrees, raise
  `PluginContractError`, fail the task, log to `errors` — never pad or truncate.
  **Not yet implemented**; belongs in the plugin runner so it applies uniformly
  across all three transports.
- **Validation the core must enforce on receipt:** non-empty namespace/label;
  `confidence` in `[0.0, 1.0]` (error, not clamp); normalized region coords;
  duplicates collapse idempotently.
- **Ship only three thin reference plugins**: `core.exif-entities`,
  `core.exif-gps`, `example.stub-classifier`. Do **not** build face recognition
  or object detection in the core — those are downstream plugins, and the core's
  job is to make them trivially droppable.

---

## 9. Getting this on GitHub

Remote: `https://github.com/DrEPIX/File-Indexer.git` (private). Nothing has been
pushed yet — there is no `.git` in `F:\File Indexer` as of writing.

```powershell
$env:Path = "C:\Program Files\Git\cmd;$env:Path"
cd "F:\File Indexer"
git config --global user.name "DrEPIX"
git config --global user.email "allmonc@fastmail.com"
git init
git add -A
git status --short | Measure-Object -Line     # expect ~70-90, NOT thousands
git commit -m "MediaEngine: core DB layer, plugin contracts, Docker scaffold"
git branch -M main
git remote add origin https://github.com/DrEPIX/File-Indexer.git
git push -u origin main
```

If the status count is in the thousands, `.venv` leaked past `.gitignore` —
stop and fix before committing. `.gitignore` already covers `.venv/`, `venv/`,
`data/`, `*.db`, `derivatives/`, `models/`, `*.safetensors`, `*.pt`, `*.bin`,
so no library index or model weight can reach the repo.

Make the PATH fix permanent:

```powershell
[Environment]::SetEnvironmentVariable("Path",
  "C:\Program Files\Git\cmd;" + [Environment]::GetEnvironmentVariable("Path","User"), "User")
```

---

## 10. Dependencies

Core (small on purpose — the engine must embed in a PyQt app without dragging in
a web stack or ML runtime): `pydantic`, `pydantic-settings`, `PyYAML`, `Pillow`,
`numpy`.

Extras: `[hash]` blake3 · `[detect]` filetype, python-magic · `[documents]`
PyMuPDF, python-docx, openpyxl, EbookLib · `[api]` fastapi, uvicorn, websockets
· `[remote]` httpx · `[vec]` sqlite-vec · `[dev]` pytest, mypy.

Every extra has a working fallback: no blake3 → sha256; no filetype → builtin
sniffer; no sqlite-vec → NumPy brute force. **Keep it that way.** External
binaries: `ffprobe`, `ffmpeg`, `exiftool`.

---

## 11. Open questions

- Codex's actual completion state for tasks A/B/C is unknown. Check
  `COORDINATION.md` before assuming anything under its owned paths works.
- Nothing has been pushed to GitHub yet.
- No pytest suite exists.
- The engine facade (`engine.py`), CLI (`cli.py`) and API (`api/`) are referenced
  by `pyproject.toml` entry points but **do not exist yet** — `pip install -e .`
  will succeed, but `mediaengine` as a console command will fail until
  milestone 2 lands `cli.py`.
