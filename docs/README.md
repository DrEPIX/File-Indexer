# MediaEngine

MediaEngine is a local-first media indexing backend. It scans files, extracts
technical metadata, creates derivatives, indexes search text, and exposes the
same engine through Python, the CLI, or the optional HTTP API.

## Five-minute local user test

From PowerShell in the repository root:

```powershell
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -e ".[hash,detect,documents,api,remote,vec]"
.\scripts\run_user_test.ps1 -LibraryPath "C:\path\to\your\media"
```

Then open `http://127.0.0.1:8420/docs`. The local-only server deliberately
requires no token. In the API docs:

1. `POST /api/scan` with `{}` starts a background scan of the configured path.
2. Copy the returned `id` into `GET /api/jobs/{job_id}` until its state is
   `done`.
3. `POST /api/search` with `{"query":"type:image"}` returns indexed images.
4. `GET /api/surface` returns every declared filter, sort, operation, and the
   capabilities the backend currently implements.
5. `GET /api/plugins` shows discovered analyzers. Start one through
   `POST /api/plugins/{plugin_id}/backfill`; it returns a job id for the same
   `GET /api/jobs/{job_id}` polling flow.

The same endpoint accepts the recursive declarative format used by the UI:

```json
{
  "where": {
    "operator": "all",
    "clauses": [
      {"key": "media.type", "operator": "eq", "value": "image"},
      {"key": "dimensions.width", "operator": "gte", "value": 1920}
    ]
  },
  "include_facets": true
}
```

This script binds only to loopback. Docker binds the process inside its
container to `0.0.0.0`, publishes it only on host loopback, and requires the
configured bearer token.

See `docs/DEPLOYMENT.md`, `docs/AI_TAGGING.md`,
`docs/FACE_REFERENCE_PACKS.md`, and `docker/README.md` for container,
local-model, face-reference, safety, and GPU profiles.

`docs/FILTER_PACKS.md` covers filter packs — installable TOML taxonomies that
add a search facet (sport, animation, origin platform, adult-content
screening) without any code. `docs/ASSISTANT.md` covers Studio's in-app
assistant, which can author those packs from a description and stages every
change for approval.

The local “Who is this?” clustering flow, manually linked profiles, and
non-generative Wikipedia biographies are documented in
`docs/PEOPLE_AND_PROFILES.md`.
