# MediaEngine QoL Contract

This is an isolated, framework-neutral contract layer. It does not own scanning,
SQLite, extraction, or plugin execution, and it does not import the future
`mediaengine` package. Claude can build those internals without merge overlap.

Its job is to keep UI/API improvement work declarative:

- `change_sheet.toml` is the only file a low-context agent normally edits.
- `SurfaceRegistry` turns the sheet into a self-describing UI manifest.
- `SearchRequest` and `QueryPlanner` validate recursive boolean filters.
- `BackendPort` is the narrow adapter the core implements later.
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
configured Python target.

## Integration boundary

The eventual core should implement `BackendPort.execute_search(SearchPlan)` and
advertise its implemented capabilities. Until a capability exists, the manifest
can still describe it and the adapter will reject attempts predictably. HTTP
controllers should be thin: parse JSON, call `QoLService`, map contract errors to
4xx responses, and return the mapping. PyQt can call the same service directly.

The planner deliberately produces a neutral plan rather than SQL. SQL compilation
belongs beside Claude's database/repository implementation, where joins, FTS,
R-tree, cursor stability, and permissions can be handled transactionally.
