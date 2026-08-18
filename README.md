# File Indexer V1 — Special Edition

File Indexer V1 is a local-first desktop library for images, videos, audio, and
documents. It recursively scans folders, extracts technical and embedded
metadata, creates previews in a separate cache, detects duplicate content,
indexes document text and plugin labels, and provides fast structured search.
Original files are opened read-only and are never modified.

## Start the desktop app

For the modern thumbnail-first experience, double-click **`Start File Indexer
Studio.bat`**. Studio uses PySide6/Qt for rounded native rendering, animated
controls, responsive media tiles, automatic folder watching, and silent video
hover previews. It opens the same local library as V1 and never requires a CLI.
Studio now includes a visual folder manager, a searchable AI Analyzer Store,
five accessible color palettes, adjustable tile density, motion/video-preview
preferences, and a safe preference reset. Removing a library folder forgets
only its index entries and generated previews; original media is never deleted.

The stable classic interface remains available through **`Start File Indexer
V1.bat`**; Studio is an additional frontend and does not overwrite it.

On this checkout, double-click **`Start File Indexer V1.bat`**. The launcher
uses the existing `.venv` and opens the GUI with `pythonw.exe`, so no console
window is required. You can also double-click `File Indexer V1.pyw` when `.pyw`
files are associated with Python.

The first launch opens a short visual tour and then offers to add a media
folder. The tour and searchable Help Center remain available inside the app.
From then on the desktop app can:

- add and recursively scan folders with live progress and safe cancellation;
- search filenames, extracted text, tags, and arbitrary analyzer labels;
- filter by media type and use the shared query syntax (`type:image`,
  `camera:Canon`, `after:2024`, `has:gps`, `color.dominant:blue`);
- inspect previews, file locations, technical metadata, and labels;
- open files or reveal them in Explorer;
- add user-owned labels that machine analyzers cannot overwrite;
- review scan history and extraction errors;
- run enabled analyzers and perform a database integrity check;
- change library roots and index/cache locations from Settings;
- switch between Claude Light, Midnight Ink, Sage Studio, High Contrast, and
  the opt-in V1 Neo style with rounded controls and arrowless scrollbars;
- adjust text from 85–140%, choose layout density, or reduce motion;
- adapt from a full desktop workspace down to a compact 900×620 window.

Useful keyboard shortcuts:

- `Ctrl+K` focuses the library search;
- `Ctrl+L` returns to Library;
- `Ctrl+,` opens Settings;
- `F1` opens the searchable Help Center;
- `Esc` clears the search on the active page.

V1 stores its configuration and index under
`%LOCALAPPDATA%\File Indexer V1` by default. The GUI embeds the backend
directly; neither the CLI nor the HTTP server is involved.

## Install from source

Python 3.11+ is required. The repository's environment is already prepared on
the development machine. For a new machine:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[hash,detect,documents,api,remote,vec,studio,dev]"
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

## Build the Windows installer

Install PyInstaller in the development environment once, then build the full
MSI. The script downloads a pinned WiX toolchain into the ignored `.tools`
directory when needed. End users do not need Python or a terminal.

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\scripts\build_installer.ps1 -Clean
```

The result is `dist\installer\File Indexer V1 Setup.msi`. It installs the app,
Start Menu shortcut, desktop shortcut, and Apps & Features uninstaller for the
current Windows user. Optional AI model services and their multi-gigabyte
weights remain external.
