# AI Collaboration Log

Permanent, human-readable coordination log for the agents working in this
workspace. **Do not delete this file.** Append new entries; do not rewrite or
remove another agent's entry.

## Protocol

1. Start each entry with local timestamp, agent name, and status.
2. State the exact directories/files you currently own or are editing.
3. Treat another agent's active paths as read-only until it releases them.
4. Ask integration questions here and answer them in a new entry.
5. Record handoffs and incompatible assumptions explicitly.
6. User-authored `AGENTS.md` and `media-engine-prompt.md` remain shared read-only inputs.

---

## 2026-08-07 15:36 America/Chicago — Codex — ACTIVE

### Ownership / no-touch request

I am exclusively editing:

- `qol_contract/**`
- this append-only coordination file, `AI_COLLABORATION_LOG.md`

I will not edit `mediaengine/**`, root `tests/**`, root `docs/**`, root
`pyproject.toml`, or other implementation paths you create. Claude, please do
not edit `qol_contract/**`; consume it through the adapter boundary or leave a
request here.

### What I built

`qol_contract/` is a dependency-free Python 3.11 package providing:

- an agent-editable `change_sheet.toml` defining 29+ filters, sorts, UI defaults,
  capability gates, and the intended control-plane operations;
- typed recursive boolean search request/plan models;
- whitelist validation from public filter keys to trusted backend field names;
- a framework-neutral `BackendPort` Protocol and `QoLService` façade;
- dynamic surface manifest and JSON Schema export;
- unit tests and integration notes.

The planner intentionally stops at a neutral `SearchPlan`; it does not generate
SQL or import `mediaengine`. This keeps SQLite joins, FTS, R-tree, cursor rules,
and repository ownership with your backend implementation.

### Contract proposed to Claude

Please implement a thin adapter (location of your choice) with:

```python
class BackendPort(Protocol):
    def execute_search(self, plan: SearchPlan) -> Mapping[str, Any]: ...
    def get_asset(self, asset_id: int) -> Mapping[str, Any] | None: ...
    def list_facets(self, namespace: str | None = None) -> Mapping[str, Any]: ...
    def capabilities(self) -> frozenset[str]: ...
```

The neutral fields currently referenced by the sheet include real schema paths
(`assets.media_type`, `technical_metadata.width`) and a few computed adapter
fields (`computed.has_location`, `computed.orientation_class`,
`computed.file_copy_count`, `metadata.camera_make`, `search.relevance`). The
adapter should map these identifiers to safe repository/query-builder methods;
never interpolate them from the client.

### Questions for Claude

1. What exact paths are you currently editing? I will add them to my no-touch list.
2. Does your search layer already have a query AST/model? If so, name its module
   and model so I can provide a non-invasive conversion adapter in my directory.
3. Which capabilities are complete or planned under these names: `structured`,
   `fts`, `facets`, `annotations`, `tags`, `spatial`, `vector`, `identities`?
4. Are you using Pydantic v1 or v2, and do you want the contract to remain
   dependency-free or mirror your Pydantic request types at the edge?
5. Please flag any backend field names in `qol_contract/change_sheet.toml` that
   conflict with your repository vocabulary.

### Next Codex actions

- validate the sheet and run the isolated tests;
- fix only `qol_contract/**` defects;
- periodically inspect this log and newly created paths for your response;
- append a tested handoff and exact results here.

---

## 2026-08-07 15:37 America/Chicago — Codex — CHECKPOINT

No separate Claude-authored coordination/status file is visible yet, including
hidden files. I will keep checking during this task.

Validation results for my isolated area:

- change sheet: valid (`32 filters`, `6 sorts`, `16 operations`);
- unit tests: `5/5 passed` under Python 3.11+ stdlib only;
- files outside my declared ownership changed by Codex: this log only;
- Git status unavailable because the workspace is not a Git repository yet.
