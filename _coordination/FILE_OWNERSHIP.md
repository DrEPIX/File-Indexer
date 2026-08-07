# File ownership

Hard rule: **do not write to a path another agent owns.** There is no merge
mechanism between the cloud sandbox and the local machine — last write wins and
the other agent's work is gone.

## Claude owns

```
mediaengine/**              entire Python package (core, db, plugins, search,
                            workers, api, config.py, engine.py, cli.py)
tests/**                    unit + integration suite
pyproject.toml
config.example.yaml
.gitignore / .dockerignore
```

## Codex owns

```
docker/**                   Dockerfiles, compose, entrypoints, healthchecks
examples/**                 reference plugins that are NOT shipped in-package
plugins-available/**        drop-in plugin manifests + their source
scripts/**                  local dev / push helpers
docs/DEPLOYMENT.md          container-specific operator docs
```

## Shared, coordinate before editing

```
_coordination/COORDINATION.md   append-only; both append to the message log
docs/README.md                  Claude drafts, Codex may append a Docker section
docs/ARCHITECTURE.md            Claude
docs/PLUGINS.md                 Claude (Codex: post corrections, don't edit)
docs/API.md                     Claude
docs/DATA.md                    Claude
```

## Notes

- `mediaengine/plugins/builtin/` holds only the three thin reference analyzers
  the spec mandates. Real models live in `plugins-available/` as out-of-process
  or HTTP plugins. That boundary is deliberate — keep it.
- If you genuinely need a core change, post in the message log. I'd rather make
  the change myself than resolve a conflict later.
