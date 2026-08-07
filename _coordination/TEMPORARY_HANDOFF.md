# TEMPORARY_HANDOFF_TO_CODEX — milestone 2 only

**Written:** 2026-08-07 by Claude (Claude Code, local, `F:\File Indexer`)
**Reason:** credit-limit contingency. Checkpoint taken mid-milestone-2 before
implementation continued.
**Scope released:** *temporarily* — `mediaengine/**` and `tests/**`, **milestone 2
only**. Nothing else moves. `pyproject.toml`, `config.example.yaml`,
`docs/ARCHITECTURE.md|PLUGINS.md|API.md|DATA.md`, `CLAUDE.md` and
`PROJECT_STATE.md` stay with Claude and are **not** released.

**Ownership is not transferred.** This is a loan. When Claude resumes it takes
`mediaengine/**` and `tests/**` back immediately; Codex stops at that point, even
mid-file, and appends what it changed to the log at the bottom of this file.

**Activation condition:** this handoff is live **only if Claude's session stopped
before appending a `[m2 complete]` entry to `COORDINATION.md`.** If that entry
exists, ignore this whole document — the loan never activated.

---

## 1. Exact file state right now

### Created by Claude this session

```
mediaengine/core/globs.py        NEW, complete, 6.5 KB — include/exclude glob
                                 matching. Not yet imported by anything.
.venv/                           NEW, gitignored. Project virtualenv (see §3).
_coordination/TEMPORARY_HANDOFF.md   NEW — this file.
```

**That is the entire delta.** No existing file was modified. `mediaengine/core/`
has no `__init__.py` yet, so the subpackage is not importable — that is the
single next step (§6).

### Untracked but pre-existing (not Claude's work this session)

`CLAUDE.md`, `_coordination/PROJECT_STATE.md` — both were already untracked when
the session started.

### Modified by Codex, untouched by Claude

`docker/api.Dockerfile`, `docker/compose.yaml`, `examples/subprocess-plugin/run.py`,
`examples/subprocess-plugin/test_harness.py`, `plugins-available/clip-http/{model,server,test_contract}.py`,
`plugins-available/clip-http/plugin.toml`.

---

## 2. Completed behaviour

**Milestone 1** — unchanged, complete, as described in `PROJECT_STATE.md §2`.
Claude did not touch it.

**Milestone 2** — one file of foundation, no runnable behaviour yet.

`mediaengine/core/globs.py` provides:

- `compile_glob(pattern, *, case_sensitive=None)` — glob → anchored regex.
  Supports `*`, `?`, `[abc]`, `[!abc]`, `**/` (zero or more directory levels),
  `**` (anything incl. separators). Case sensitivity defaults from
  `os.path.normcase`, so it is case-insensitive on Windows.
- `to_relative_posix(path, root)` — root-relative forward-slash form; falls back
  to the absolute posix path when `path` escapes `root` via a symlink.
- `GlobMatcher(include, exclude)` — `.matches_file(rel)`, `.excludes_dir(rel)`,
  `.filter(iterable)`. Deny wins over allow. Exclude patterns ending `/**` or
  `/*` additionally compile a directory form with the suffix stripped, so
  `**/node_modules/**` prunes the directory itself instead of rejecting its
  contents one file at a time.

**Verification status: none.** No test has been written or run against it. Treat
it as unverified code, not as working code.

---

## 3. Environment / process state

Nothing is running. The dependency install finished and exited 0.

- Project venv: `F:\File Indexer\.venv` (gitignored). **Use it for everything:**
  `.\.venv\Scripts\python.exe`.
- `python` on PATH resolves to the **WindowsApps stub** in both PowerShell and
  Git Bash and fails with "Python was not found". Always use the venv
  interpreter explicitly, or `C:\Users\charl.THE_CITADEL-V2\miniconda3\python.exe`.
- Python 3.13.5 · SQLite 3.50.2 (so `contentless_delete` *would* have been
  available — the external-content FTS decision in `PROJECT_STATE.md §3` still
  stands on its own merits and should not be revisited).
- Installed in the venv and importable: `pydantic`, `pydantic-settings`, `yaml`,
  `PIL`, `numpy`, `blake3`, `filetype`, `fitz`/PyMuPDF, `docx`, `openpyxl`,
  `ebooklib`, `fastapi`, `uvicorn`, `websockets`, `httpx`, `sqlite_vec`,
  `pytest`, `pytest-asyncio`, `mypy`, `types-PyYAML`.
- `ffmpeg` and `ffprobe`: present at `C:\ffmpeg\bin` (on PATH).
- **`exiftool` is NOT installed and `winget` is not available.** The image
  extractor therefore needs a complete Pillow-based EXIF fallback, not a stub —
  on this machine the fallback *is* the only path that will ever run. Do not
  write code that assumes exiftool exists.
- `python-magic` is **not** installed (it needs a libmagic DLL on Windows).
  `filetype` is installed. The builtin magic-byte sniffer must stand alone.

## 4. Commands run and their results

| Command | Result |
|---|---|
| `miniconda3\python.exe -m venv .venv` | ok |
| `.venv\Scripts\python.exe -m pip install <all deps>` | exit 0 |
| import probe of all 22 dependencies | all OK (`fitz` warns it is deprecated in favour of `import pymupdf`) |
| `ffprobe -version` | ffprobe 2025-12-24-git-abb1524138-full_build |
| `git status --short` | as listed in §1 |

**No test suite has been run. `tests/` still does not exist.** Every claim in
`PROJECT_STATE.md §6` about milestone 1 remains ad-hoc manual verification.

## 5. Known failures / gaps

1. `mediaengine/core/__init__.py` missing → `mediaengine.core` is not importable.
2. `mediaengine/engine.py` and `mediaengine/cli.py` still do not exist, so the
   `mediaengine` console script declared in `pyproject.toml` fails. Unchanged
   from `PROJECT_STATE.md §11`.
3. `globs.py` is untested.
4. `pyproject.toml` declares `readme = "docs/README.md"`, which does not exist —
   a source build would fail. Claude owns that file; leave it.

---

## 6. Next smallest atomic step

**Create `mediaengine/core/__init__.py`** exporting `GlobMatcher`, `compile_glob`,
`to_relative_posix`, then `tests/core/test_globs.py` covering: `**/` spanning
zero directories, `**/node_modules/**` pruning the directory itself, deny beating
allow, case-insensitivity on Windows, and an unterminated `[` treated as a
literal. Run `.\.venv\Scripts\python.exe -m pytest tests/core/test_globs.py`.

That is one file plus one test file, and it makes the first piece of milestone 2
verifiable. Do not start the walker until it passes.

### Then, in order (the milestone-2 plan `PROJECT_STATE.md §7` expands on)

```
core/identity.py     hash + magic-byte type detection + dhash
core/procs.py        subprocess with hard timeout and process-group kill
core/walker.py       resumable bounded-queue walk
core/extractors/     ffprobe · exiftool-daemon-with-Pillow-fallback · documents
core/derivatives.py  thumbnails · keyframes · proxy · audio
core/sidecars.py     .xmp / RAW+JPEG / motion photos
core/pipeline.py     stage orchestration
engine.py, cli.py    facade + `scan` / `stat` / `migrate`
```

---

## 7. Design decisions already made — preserve these

These were settled before the checkpoint. They are not open questions.

1. **`content_hash` is prefixed and self-describing:** `b3:<hex>` for BLAKE3,
   `sha256:<hex>` for the fallback. `CONTRACTS.md §2` already shows the `b3:`
   form on the wire, and the prefix stops two algorithms from colliding in the
   `assets.content_hash` UNIQUE index. `assets.hash_algorithm` keeps the bare
   name.
2. **Derivative paths must sanitise that colon** — `:` is illegal in a Windows
   filename. Layout is
   `derivatives/<hex[0:2]>/<content_hash with ':' → '_'>/thumb_512.webp`.
3. **Thread pools, not `ProcessPoolExecutor`,** for metadata extraction and
   derivative generation — a deliberate deviation from the sketch in
   `PROJECT_STATE.md §7`. Every extractor is either a subprocess wait
   (ffprobe/ffmpeg/exiftool) or a Pillow C loop, and Pillow releases the GIL
   around decode/encode; meanwhile Windows `spawn` re-imports the world per
   worker. The derivative worker is still written as a **module-level pure
   function of plain data**, so switching to processes stays a one-line change
   in the pool factory. This tradeoff is to be recorded in
   `docs/ARCHITECTURE.md` (Claude's file — Codex: note it here instead).
4. **Magic bytes decide the media type; the extension is only a tiebreaker**
   within a confirmed container family (TIFF → CR2/NEF/ARW/DNG, ZIP → docx/xlsx/
   pptx/epub). Never type a file from its extension alone.
5. Milestone 1's architecture rules in `CLAUDE.md` are binding: single writer
   thread, repositories are the only SQL, write methods take an optional `conn`,
   plugins never write to the database.

---

## 8. Rules for Codex while the loan is active

1. **Milestone 2 only.** Do not start milestone 3+ in `mediaengine/**`.
2. **Do not edit** `pyproject.toml`, `config.example.yaml`, `CLAUDE.md`,
   `_coordination/PROJECT_STATE.md`, `_coordination/CONTRACTS.md`, or
   `docs/ARCHITECTURE.md|PLUGINS.md|API.md|DATA.md`. If you need a change in one,
   write the request in §9 below and work around it.
3. **Preserve §7 verbatim.** If one of those decisions turns out to be wrong,
   stop and write it in §9 rather than quietly choosing differently — a silent
   divergence costs a rewrite at integration.
4. **Log every file you touch in §9,** one line each, as you go, not at the end.
5. **Stop the moment Claude resumes,** even mid-file, and leave the code in a
   state that imports.
6. `mediaengine/plugins/builtin/` stays limited to the three reference analyzers.

---

## 9. Codex change log (append below; Codex only)

- [16:41] HANDOFF ACTIVATED — Claude session confirmed idle before milestone 2
  completion; Codex temporarily accepts only the released milestone-2 scope.
  Existing `mediaengine/core/globs.py` is preserved unchanged pending tests.
- [16:43] HANDOFF DEACTIVATED — user instructed Claude to continue. Codex made
  no changes under `mediaengine/**` or `tests/**`; both scopes returned intact.

<!-- format: - [HH:MM] path/to/file — what changed, why -->
