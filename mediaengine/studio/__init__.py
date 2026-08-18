"""File Indexer Studio: the Qt-based, thumbnail-first desktop frontend."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QMessageBox
    except ImportError:
        # This path is mainly for source checkouts that did not install the
        # optional Studio dependency; packaged builds always bundle Qt.
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        messagebox.showerror(
            "File Indexer Studio needs Qt",
            "Install the Studio desktop dependency with:\n\n"
            "python -m pip install -e .[studio]\n\n"
            "The existing File Indexer V1 application is still available.",
        )
        root.destroy()
        return 1

    from ..gui.config_store import load_desktop_config
    from .window import build_application

    try:
        app, window = build_application(load_desktop_config())
        window.show()
        return int(app.exec())
    except Exception as exc:
        QMessageBox.critical(None, "File Indexer Studio", f"Studio could not start:\n\n{exc}")
        return 1


__all__ = ["main"]
