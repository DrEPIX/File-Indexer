# JSONL Subprocess Analyzer Example

This directory is a copy-paste starting point for a standalone MediaEngine
analyzer. It has no dependency on the engine package and uses only Python 3.11+
standard-library modules.

Register the directory in `plugins.directories`. The host reads `plugin.toml`,
starts `python -u run.py`, performs the `hello`/`ready` handshake, and then sends
one `analyze` request at a time. `run.py` demonstrates derivative path access,
prior annotation consumption, a permanent error response, and clean shutdown.

Test it exactly as a host would:

```powershell
python examples/subprocess-plugin/test_harness.py
```

Protocol stdout must contain JSONL only. Use `log()` (stderr) for diagnostics.
If you add a library that logs to stdout, reconfigure it before the handshake;
one banner or progress bar on stdout corrupts the channel.

To build a real plugin:

1. Change the permanent plugin id, version, model id, emitted namespaces, and
   minimum capability declarations in both `plugin.toml` and `MANIFEST`.
2. Replace `analyze()` while preserving `request_id` echoing and response types.
3. Normalize region coordinates to 0..1 and keep confidence in 0..1.
4. Bump `version` only when output semantics change; a bump re-analyzes the
   library.
5. Keep originals and derivatives read-only and never write the engine DB.

