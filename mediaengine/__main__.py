"""``python -m mediaengine`` — the same entry point as the console script.

Container entrypoints prefer this form because it works without the script
being on ``PATH``, which is the usual outcome of ``pip install --user`` inside
an image.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
