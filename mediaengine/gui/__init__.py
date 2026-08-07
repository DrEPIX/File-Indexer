"""File Indexer V1 desktop application.

The GUI embeds :class:`mediaengine.engine.MediaEngine` directly.  It does not
start the HTTP API and does not shell out to the command-line client, which
keeps the desktop and backend lifecycle in one process.
"""

from .app import FileIndexerV1, main

__all__ = ["FileIndexerV1", "main"]
