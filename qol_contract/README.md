# MediaEngine QoL Contract

This is an isolated, framework-neutral contract layer. It does not own scanning,
SQLite, extraction, or plugin execution. The planner remains independent of the
core, while `MediaEngineBackend` is the narrow, allowlisted adapter used by the
running HTTP service.

Its job is to keep UI/API improvement work declarative:

- `change_sheet.toml` is the only file a low-context agent normally edits.
- `SurfaceRegistry` turns the sheet into a self-describing UI manifest.
- `SearchRequest` and `QueryPlanner` validate recursive boolean filters.
- `BackendPort` keeps callers independent of the concrete engine adapter.
- `QoLService` exposes the same behavior to an in-process GUI or HTTP routes.
- JSON Schema export lets forms, SDKs, and smaller models discover valid keys.

## Safe edit recipe

To add a filter, copy one `[[filters]]` block in `change_sheet.toml` and change:

1. `key` — permanent public name; do not rename existing keys.
2. `field` — stable backend adapter field, not raw user-supplied SQL.
3. `type`, `operators`, `widget`, and `capability`.
4. `facet = true` if the UI should show counts.

No clause ever supplies a SQL column directly. The registry maps public keys to
trusted backend fields, so frontends and small models cannot inject a field name.

## Validate and export

From this directory, with Python 3.11+:

```powershell
$env:PYTHONPATH = "src"
python -m mediaengine_qol --sheet change_sheet.toml validate
python -m mediaengine_qol --sheet change_sheet.toml export --output generated/surface.json
python -m unittest discover -s tests -v
```

The generated `surface.json` is disposable output; the TOML sheet is canonical.

From the repository root, the preferred complete quality gate is:

```powershell
.\scripts\verify.ps1
```

It validates every plugin manifest, parses the PowerShell tooling, compiles the
source tree, and includes the core, QoL, analyzer, and authoring-tool tests in
one run. Use `-Quick` to skip bytecode compilation, `-Smoke` to exercise a
disposable mixed-media library end to end, or `-FullTypeCheck` when the full
application's optional dependency stubs are installed and compatible with the
configured Python target. After building the Windows release, add
`-PackagedApp` to launch the frozen GUI twice against isolated LocalAppData and
verify recovery plus SQLite integrity.

For a heavier duplicate-publication stress check, run
`.\scripts\smoke_backend.ps1 -DuplicateCount 100`.

The normal Windows build is the onedir payload consumed by the MSI. A portable
single executable remains available independently:

```powershell
.\scripts\build_v1.ps1 -Clean -OneFile
.\scripts\smoke_packaged_app.ps1 -ExecutablePath '.\dist\portable\File Indexer V1.exe'
```

## Integration boundary

`MediaEngineBackend` implements `BackendPort.execute_search(SearchPlan)` and
advertises the capabilities available in the running core. Capabilities that are
not wired yet, such as query-vector production, are rejected predictably before
execution. HTTP controllers stay thin: parse JSON, call `QoLService`, map
contract errors to 4xx responses, and return the mapping. A Python GUI can call
the same service directly.

The planner deliberately produces a neutral plan rather than SQL. SQL compilation
is confined to the concrete adapter, where joins, FTS, R-tree filtering, strict
cursors, and dynamic facets can be tested without leaking database fields into
the public contract.
