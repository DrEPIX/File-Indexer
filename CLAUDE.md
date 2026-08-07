# MediaEngine — project instructions

Read this first. Then read `_coordination/PROJECT_STATE.md` for full detail and
`_coordination/CONTRACTS.md` for the frozen plugin wire contracts.

## What this is

A local-first, plugin-driven media indexing engine in Python 3.11+. It powers a
personal photo/video/document library — think Immich or digiKam, but with a
first-class extension API so analyzers can be written independently and added
later **without touching the core**.

**The single most important property: the core knows nothing about what tags
exist.** It stores, indexes and searches arbitrary namespaced annotations
produced by plugins. Someone must be able to ship an analyzer emitting
`garment.color=red` or `animal.species=dog` and have it become searchable and
facetable with zero core changes. Do not implement any specific taxonomy.
Implement the machinery that makes taxonomies pluggable.

The full original specification is `media-engine-prompt.md` in this directory.

## Non-negotiable principles

These drive most design decisions. Violating one is a bug even if tests pass.

1. **Never modify originals.** Read-only access to source files, always. All
   derivatives go to a separate cache directory.
2. **Content identity, not path identity.** A moved or renamed file is the same
   asset. Two copies of the same bytes are one asset with two paths.
3. **Provenance on everything derived.** Every annotation records which producer
   (plugin id + version + model id + config hash) made it, when, and with what
   confidence. Nothing derived is anonymous.
4. **User data outranks machine data.** A human correction survives re-indexing,
   model upgrades, and full re-analysis. No plugin may ever overwrite a
   user-confirmed value, at any confidence.
5. **Everything is resumable and idempotent.** Interrupt anywhere; restart
   continues. Re-running a producer over an already-processed asset is a no-op
   unless the producer version changed.
6. **Everything derived is purgeable.** Delete all output from one producer, or
   all derived data for one asset, in a single transaction — and re-derive it.
7. **Local by default.** No network egress from core or plugins unless a plugin
   declares the `network` capability *and* the operator enables it in config.

## Two agents work in this repo

Claude owns `mediaengine/**`, `tests/**`, `pyproject.toml`, root config.
Codex owns `docker/**`, `examples/**`, `plugins-available/**`, `scripts/**`,
`docs/DEPLOYMENT.md`.

**Do not edit files the other agent owns.** See
`_coordination/FILE_OWNERSHIP.md`. Coordinate by appending to the message log in
`_coordination/COORDINATION.md` — it is append-only, never rewrite it.
`_coordination/CONTRACTS.md` is frozen at v1.1; changing it requires a message
and an explicit ack from the other agent first.

## Architecture rules that are already decided

Do not relitigate these; they are implemented and tested.

- **One writer thread.** Every database mutation is a callable submitted to
  `db.writer`. Readers use thread-local connections and never block. This
  eliminates lock contention rather than retrying it. Never open your own
  write connection.
- **Repositories are the only code that writes SQL.** Everything above speaks
  dicts and dataclasses.
- **Write methods take an optional `conn`.** Passing it runs inside the caller's
  transaction; omitting it submits to the writer and blocks. That convention is
  what lets multi-repository operations compose atomically.
- **Plugins return annotations; they never write to the database.** The core
  assigns producer ids, resolves conflicts against user data, and commits
  transactionally.
- **Supersession, not deletion.** A new producer version marks the old version's
  claims `superseded_by`. A claim the new version no longer makes is marked
  retracted by pointing `superseded_by` at the row's own id — it drops out of
  every live view while preserving the audit trail.
- **Facets are computed from the `annotations` table at query time**, grouped by
  namespace. This is the payoff of the whole design. Never hardcode a facet list.

## Environment facts (this machine)

- Git is installed at `C:\Program Files\Git\cmd\git.exe` but **is not on PATH**
  — conda's `(base)` env shadows it. Fix per shell:
  `$env:Path = "C:\Program Files\Git\cmd;$env:Path"`
- `gh` and `winget` are **not** installed.
- GPU: NVIDIA RTX 4080 16GB. Docker CLI 28.2.2 present; Docker Desktop engine
  was stopped as of last check.
- GitHub remote: `https://github.com/DrEPIX/File-Indexer.git` (private).
- `plugins-available/clip-http/.venv` is ~218MB of Codex's local install. It is
  covered by `.gitignore`. Never commit it.

## Working agreements

- Full type annotations. `mypy --strict` clean.
- Docstrings on every public symbol, explaining *why*, not restating the name.
- No stubs and no `TODO`s in anything declared complete.
- If a design decision is genuinely ambiguous, choose the simpler option,
  implement it fully, and record the tradeoff in `docs/ARCHITECTURE.md`.
- Tests must cover the nasty cases, not the happy path: resumable scan after
  `kill -9`, duplicate and moved files, a plugin that raises, a plugin that
  times out, unicode and very long filenames, zero-byte and truncated media,
  concurrent read-during-write.

## Build order

Each milestone must be runnable and demoable before the next starts.

1. ✅ Config, DB, migrations, single-writer connection layer.
2. ⬜ Walker + identity + technical metadata extraction + derivatives.
   CLI: `scan`, `stat`.
3. ⬜ Plugin contract + registry + scheduler + three reference plugins.
   CLI: `plugins list`, `backfill`.
4. ⬜ Search: FTS + structured filters + facets. CLI: `search`.
5. ⬜ Spatial + vector search.
6. ⬜ Identities/clusters layer.
7. ⬜ FastAPI + WebSocket + OpenAPI.
8. ⬜ Out-of-process plugin host.
9. ⬜ Docs + worked example: index a folder, write a toy plugin, watch its
   labels appear as search facets.

Milestone 1 is complete and tested. Start at milestone 2. `PROJECT_STATE.md`
has the detailed brief for it.
