# File Indexer V1 — Special Edition

File Indexer V1 is a local-first desktop library for images, videos, audio, and
documents. It recursively scans folders, extracts technical and embedded
metadata, creates previews in a separate cache, detects duplicate content,
indexes document text and plugin labels, and provides fast structured search.
Original files are opened read-only and are never modified.

## Start the desktop app

On this checkout, double-click **`Start File Indexer V1.bat`**. The launcher
uses the existing `.venv` and opens the GUI with `pythonw.exe`, so no console
window is required. You can also double-click `File Indexer V1.pyw` when `.pyw`
files are associated with Python.

The first launch asks for a media folder. From then on the desktop app can:

- add and recursively scan folders with live progress and safe cancellation;
- search filenames, extracted text, tags, and arbitrary analyzer labels;
- filter by media type and use the shared query syntax (`type:image`,
  `camera:Canon`, `after:2024`, `has:gps`, `color.dominant:blue`);
- inspect previews, file locations, technical metadata, and labels;
- open files or reveal them in Explorer;
- add user-owned labels that machine analyzers cannot overwrite;
- review scan history and extraction errors;
- run enabled analyzers and perform a database integrity check;
- change library roots and index/cache locations from Settings.

V1 stores its configuration and index under
`%LOCALAPPDATA%\File Indexer V1` by default. Placing a `config.yaml` beside a
packaged executable switches it to portable mode. The GUI embeds the backend
directly; neither the CLI nor the HTTP server is involved.

## Install from source

Python 3.11+ is required. The repository's environment is already prepared on
the development machine. For a new machine:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[hash,detect,documents,api,remote,vec,dev]"
```

`ffmpeg`/`ffprobe` improve video and audio extraction. ExifTool is optional;
the Pillow fallback handles images when it is unavailable.

## Verify the build

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The standalone backend remains available through the documented Python API,
CLI, and optional FastAPI service. See [backend and API usage](docs/README.md)
and [deployment](docs/DEPLOYMENT.md).
