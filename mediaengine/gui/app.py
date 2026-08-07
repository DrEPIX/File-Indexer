"""Native desktop interface for File Indexer V1.

Tk/ttk is part of the Python runtime, so this surface adds no GUI framework to
the backend dependency graph.  Long-running work stays off Tk's event thread;
all engine writes still flow through MediaEngine's single database writer.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    BooleanVar,
    Canvas,
    Listbox,
    Menu,
    StringVar,
    Text,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    simpledialog,
    ttk,
)
from typing import Any, Literal, cast

from PIL import Image, ImageOps, ImageTk

from .. import __version__
from ..config import Config
from ..core.control import CancelToken, ProgressEvent
from ..engine import MediaEngine
from ..errors import MediaEngineError, NotFoundError
from ..search import Query, parse_query
from ..util import human_bytes, setup_logging
from .config_store import APP_NAME, load_desktop_config, save_desktop_config

_LOG = logging.getLogger(__name__)

BG = "#0b1020"
PANEL = "#121a2e"
PANEL_2 = "#18233b"
TEXT = "#edf2ff"
MUTED = "#9aa9c5"
ACCENT = "#ff6b4a"
ACCENT_DARK = "#d94b30"
TEAL = "#4dd7c8"
BORDER = "#263653"
ERROR = "#ff7387"


def format_bytes(value: object) -> str:
    """Format an arbitrary database value as a human-readable byte count."""
    try:
        if value is None:
            return cast(str, human_bytes(0))
        if isinstance(value, (int, float, str)):
            return cast(str, human_bytes(float(value)))
        return "—"
    except (TypeError, ValueError):
        return "—"


def display_value(value: object) -> str:
    """Compact a value for the metadata inspector."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def safe_derivative_path(config: Config, relative: object) -> Path | None:
    """Resolve a derivative row without allowing it to escape the cache root."""
    if not relative:
        return None
    root = config.storage.derivatives_path.resolve()
    candidate = (root / str(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class FileIndexerV1:
    """Own the V1 window, its MediaEngine instance, and background jobs."""

    def __init__(self, root: Tk, config: Config | None = None) -> None:
        self.root = root
        self.config = config or load_desktop_config()
        self.engine = MediaEngine(self.config)
        self.events: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self.worker_lock = threading.Lock()
        self.workers: set[threading.Thread] = set()
        self.cancel_token: CancelToken | None = None
        self.long_job: threading.Thread | None = None
        self.close_pending = False
        self.search_generation = 0
        self.search_offset = 0
        self.page_size = 100
        self.current_total = 0
        self.result_rows: dict[str, dict[str, Any]] = {}
        self.thumbnail_image: ImageTk.PhotoImage | None = None
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.pages: dict[str, ttk.Frame] = {}

        self.status_text = StringVar(value="Starting the library…")
        self.progress_text = StringVar(value="Ready")
        self.search_text = StringVar()
        self.type_filter = StringVar(value="all")
        self.result_count = StringVar(value="0 items")
        self.page_text = StringVar(value="Page 1")
        self.derive_var = BooleanVar(value=True)
        self.rehash_var = BooleanVar(value=False)
        self.db_path_var = StringVar(value=str(self.config.storage.db_path))
        self.cache_path_var = StringVar(value=str(self.config.storage.derivatives_path))

        self._configure_window()
        self._configure_style()
        self._build_shell()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(60, self._drain_events)
        self._start_engine()

    # ── window and theme ──────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} — Special Edition")
        self.root.geometry("1320x820")
        self.root.minsize(1040, 680)
        self.root.configure(bg=BG)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL_2)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground=TEXT)
        style.configure("Hero.TLabel", font=("Segoe UI Semibold", 30), foreground=TEXT)
        style.configure("CardValue.TLabel", background=PANEL, font=("Segoe UI Semibold", 22), foreground=TEXT)
        style.configure("CardLabel.TLabel", background=PANEL, foreground=MUTED)
        style.configure("TButton", background=PANEL_2, foreground=TEXT, padding=(13, 8), borderwidth=0)
        style.map("TButton", background=[("active", BORDER), ("pressed", "#314362")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(14, 9))
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("pressed", "#b83b26")])
        style.configure("Nav.TButton", anchor="w", background=BG, padding=(18, 12))
        style.map("Nav.TButton", background=[("active", PANEL_2)])
        style.configure("Selected.Nav.TButton", anchor="w", background=PANEL_2, foreground=TEAL, padding=(18, 12))
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=32, borderwidth=0)
        style.map("Treeview", background=[("selected", "#244963")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, relief="flat", padding=8)
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER, padding=8)
        style.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2, foreground=TEXT, arrowcolor=TEXT, padding=6)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 9))
        style.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", background=TEAL, troughcolor=PANEL_2, borderwidth=0)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TSeparator", background=BORDER)

    def _build_shell(self) -> None:
        header = ttk.Frame(self.root, style="Panel.TFrame", padding=(22, 14))
        header.pack(fill="x")
        brand = ttk.Frame(header, style="Panel.TFrame")
        brand.pack(side=LEFT)
        ttk.Label(brand, text="FILE INDEXER", style="Panel.TLabel", font=("Segoe UI Semibold", 16)).pack(side=LEFT)
        ttk.Label(brand, text=" V1 ", background=ACCENT, foreground="#ffffff", font=("Segoe UI Semibold", 9)).pack(side=LEFT, padx=8)
        ttk.Label(brand, text="SPECIAL EDITION", style="PanelMuted.TLabel", font=("Segoe UI", 9)).pack(side=LEFT)
        ttk.Label(header, textvariable=self.status_text, style="PanelMuted.TLabel").pack(side=RIGHT)

        body = ttk.Frame(self.root)
        body.pack(fill=BOTH, expand=True)
        sidebar = ttk.Frame(body, width=190, padding=(10, 20))
        sidebar.pack(side=LEFT, fill="y")
        sidebar.pack_propagate(False)
        for key, label in (
            ("library", "▦   Library"),
            ("activity", "◷   Activity"),
            ("plugins", "◇   Analyzers"),
            ("settings", "⚙   Settings"),
        ):
            button = ttk.Button(
                sidebar,
                text=label,
                style="Nav.TButton",
                command=partial(self.show_page, key),
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button
        ttk.Label(sidebar, text=f"Engine {__version__}", style="Muted.TLabel").pack(side="bottom", pady=10)

        self.content = ttk.Frame(body, padding=(4, 8, 18, 8))
        self.content.pack(side=LEFT, fill=BOTH, expand=True)
        self.pages["library"] = self._build_library_page(self.content)
        self.pages["activity"] = self._build_activity_page(self.content)
        self.pages["plugins"] = self._build_plugins_page(self.content)
        self.pages["settings"] = self._build_settings_page(self.content)

        footer = ttk.Frame(self.root, style="Panel.TFrame", padding=(18, 8))
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(footer, mode="determinate", length=240)
        self.progress.pack(side=LEFT, padx=(0, 12))
        ttk.Label(footer, textvariable=self.progress_text, style="PanelMuted.TLabel").pack(side=LEFT)
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.cancel_job, state="disabled")
        self.cancel_button.pack(side=RIGHT)
        self.show_page("library")

    # ── page construction ─────────────────────────────────────────────────

    def _page(self, parent: ttk.Frame, title: str, subtitle: str) -> ttk.Frame:
        page = ttk.Frame(parent)
        ttk.Label(page, text=title, style="Title.TLabel").pack(anchor="w", pady=(10, 1))
        ttk.Label(page, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(0, 14))
        return page

    def _build_library_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Your library", "Search, inspect, and add media without opening a terminal.")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, 10))
        search = ttk.Entry(toolbar, textvariable=self.search_text)
        search.pack(side=LEFT, fill="x", expand=True)
        search.bind("<Return>", lambda _: self.run_search(reset=True))
        types = ttk.Combobox(toolbar, textvariable=self.type_filter, values=("all", "image", "video", "audio", "document", "other"), state="readonly", width=11)
        types.pack(side=LEFT, padx=8)
        types.bind("<<ComboboxSelected>>", lambda _: self.run_search(reset=True))
        ttk.Button(toolbar, text="Search", command=lambda: self.run_search(reset=True)).pack(side=LEFT)
        ttk.Button(toolbar, text="Add & Scan Folder", style="Accent.TButton", command=self.choose_scan_folder).pack(side=LEFT, padx=(8, 0))

        cards = ttk.Frame(page)
        cards.pack(fill="x", pady=(0, 10))
        self.card_values: dict[str, StringVar] = {}
        for key, label in (("assets", "Indexed items"), ("images", "Images"), ("videos", "Videos"), ("documents", "Documents")):
            card = ttk.Frame(cards, style="Panel.TFrame", padding=(15, 10))
            card.pack(side=LEFT, fill="x", expand=True, padx=(0, 8) if key != "documents" else 0)
            value = StringVar(value="—")
            self.card_values[key] = value
            ttk.Label(card, textvariable=value, style="CardValue.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="CardLabel.TLabel").pack(anchor="w")

        paned = ttk.Panedwindow(page, orient="horizontal")
        paned.pack(fill=BOTH, expand=True)
        results_panel = ttk.Frame(paned, style="Panel.TFrame", padding=8)
        detail_panel = ttk.Frame(paned, style="Panel.TFrame", padding=12)
        paned.add(results_panel, weight=4)
        paned.add(detail_panel, weight=2)

        top = ttk.Frame(results_panel, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, textvariable=self.result_count, style="PanelMuted.TLabel").pack(side=LEFT)
        self.results = ttk.Treeview(results_panel, columns=("name", "type", "size", "date", "dimensions"), show="headings", selectmode="browse")
        for column, label, width, anchor in (
            ("name", "Name", 285, "w"), ("type", "Type", 80, "center"),
            ("size", "Size", 85, "e"), ("date", "Captured", 145, "w"),
            ("dimensions", "Dimensions", 100, "center"),
        ):
            self.results.heading(column, text=label)
            tree_anchor = cast(Literal["w", "center", "e"], anchor)
            self.results.column(column, width=width, minwidth=55, anchor=tree_anchor)
        scroll = ttk.Scrollbar(results_panel, orient=VERTICAL, command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        self.results.pack(fill=BOTH, expand=True)
        self.results.bind("<<TreeviewSelect>>", self._result_selected)
        self.results.bind("<Double-1>", lambda _: self.open_selected())
        self.results.bind("<Button-3>", self._result_menu)
        pager = ttk.Frame(results_panel, style="Panel.TFrame")
        pager.pack(fill="x", pady=(8, 0))
        self.prev_button = ttk.Button(pager, text="← Previous", command=lambda: self.change_page(-1), state="disabled")
        self.prev_button.pack(side=LEFT)
        ttk.Label(pager, textvariable=self.page_text, style="PanelMuted.TLabel").pack(side=LEFT, padx=12)
        self.next_button = ttk.Button(pager, text="Next →", command=lambda: self.change_page(1), state="disabled")
        self.next_button.pack(side=LEFT)
        ttk.Button(pager, text="Refresh", command=self.refresh_all).pack(side=RIGHT)

        self.preview = ttk.Label(detail_panel, text="Select an item", style="PanelMuted.TLabel", anchor="center")
        self.preview.pack(fill="x", pady=(4, 12))
        detail_actions = ttk.Frame(detail_panel, style="Panel.TFrame")
        detail_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(detail_actions, text="Open", command=self.open_selected).pack(side=LEFT)
        ttk.Button(detail_actions, text="Show in folder", command=self.reveal_selected).pack(side=LEFT, padx=5)
        ttk.Button(detail_actions, text="Add label", command=self.add_label).pack(side=LEFT)
        self.detail = Text(detail_panel, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", wrap="word", font=("Consolas", 9), padx=4, pady=4, state="disabled")
        self.detail.pack(fill=BOTH, expand=True)
        return page

    def _build_activity_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Activity", "Scan history and actionable extraction errors.")
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Scan all configured folders", style="Accent.TButton", command=self.scan_configured).pack(side=LEFT)
        ttk.Button(actions, text="Refresh", command=self.refresh_activity).pack(side=LEFT, padx=8)
        notebook = ttk.Notebook(page)
        notebook.pack(fill=BOTH, expand=True)
        scans_frame = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
        errors_frame = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
        notebook.add(scans_frame, text="Scans")
        notebook.add(errors_frame, text="Errors")
        self.scans_tree = ttk.Treeview(scans_frame, columns=("id", "root", "state", "seen", "new", "errors", "started"), show="headings")
        for col, label, width in (("id", "ID", 55), ("root", "Folder", 360), ("state", "State", 90), ("seen", "Seen", 75), ("new", "New", 75), ("errors", "Errors", 70), ("started", "Started", 170)):
            self.scans_tree.heading(col, text=label)
            self.scans_tree.column(col, width=width, anchor="w")
        self.scans_tree.pack(fill=BOTH, expand=True)
        self.errors_tree = ttk.Treeview(errors_frame, columns=("time", "scope", "kind", "item", "message"), show="headings")
        for col, label, width in (("time", "Time", 160), ("scope", "Scope", 70), ("kind", "Kind", 130), ("item", "Item", 260), ("message", "Message", 420)):
            self.errors_tree.heading(col, text=label)
            self.errors_tree.column(col, width=width, anchor="w")
        self.errors_tree.pack(fill=BOTH, expand=True)
        self.errors_tree.bind("<Double-1>", self._show_error)
        return page

    def _build_plugins_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Analyzers", "Optional processors can add searchable labels without changing the core.")
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Refresh analyzers", command=self.refresh_plugins).pack(side=LEFT)
        ttk.Button(actions, text="Run selected", style="Accent.TButton", command=self.run_selected_plugin).pack(side=LEFT, padx=8)
        self.plugins_tree = ttk.Treeview(page, columns=("id", "version", "transport", "enabled", "state", "description"), show="headings")
        for col, label, width in (("id", "Analyzer", 210), ("version", "Version", 80), ("transport", "Transport", 100), ("enabled", "Enabled", 75), ("state", "Tasks", 150), ("description", "Description", 400)):
            self.plugins_tree.heading(col, text=label)
            self.plugins_tree.column(col, width=width, anchor="w")
        self.plugins_tree.pack(fill=BOTH, expand=True)
        return page

    def _build_settings_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Settings", "V1 stores its index separately and never modifies original files.")
        paths = ttk.Frame(page, style="Panel.TFrame", padding=16)
        paths.pack(fill="x", pady=(0, 12))
        ttk.Label(paths, text="Library folders", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        roots_area = ttk.Frame(paths, style="Panel.TFrame")
        roots_area.pack(fill="x", pady=8)
        self.roots_list = Listbox(roots_area, height=6, bg=PANEL_2, fg=TEXT, selectbackground="#244963", relief="flat", highlightthickness=1, highlightbackground=BORDER, font=("Segoe UI", 10))
        self.roots_list.pack(side=LEFT, fill="x", expand=True)
        root_actions = ttk.Frame(roots_area, style="Panel.TFrame")
        root_actions.pack(side=LEFT, fill="y", padx=(8, 0))
        ttk.Button(root_actions, text="Add…", command=self.add_root).pack(fill="x")
        ttk.Button(root_actions, text="Remove", command=self.remove_root).pack(fill="x", pady=6)
        for root_path in self.config.library.roots:
            self.roots_list.insert(END, str(root_path))

        storage = ttk.Frame(page, style="Panel.TFrame", padding=16)
        storage.pack(fill="x", pady=(0, 12))
        ttk.Label(storage, text="Index storage", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(storage, text="Database", style="PanelMuted.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(storage, textvariable=self.db_path_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(storage, text="Browse…", command=self.choose_database).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(storage, text="Preview cache", style="PanelMuted.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(storage, textvariable=self.cache_path_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(storage, text="Browse…", command=self.choose_cache).grid(row=2, column=2, padx=(8, 0))
        storage.columnconfigure(1, weight=1)

        options = ttk.Frame(page, style="Panel.TFrame", padding=16)
        options.pack(fill="x")
        ttk.Label(options, text="Scan options", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(0, 7))
        ttk.Checkbutton(options, text="Create previews and thumbnails", variable=self.derive_var).pack(anchor="w")
        ttk.Checkbutton(options, text="Force content re-hash on the next scan", variable=self.rehash_var).pack(anchor="w", pady=4)
        row = ttk.Frame(page)
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side=LEFT)
        ttk.Button(row, text="Open data folder", command=lambda: self._open_path(self.config.storage.db_path.parent)).pack(side=LEFT, padx=8)
        ttk.Button(row, text="Run integrity check", command=self.run_integrity_check).pack(side=LEFT)
        return page

    # ── lifecycle and background dispatch ─────────────────────────────────

    def _start_engine(self) -> None:
        def work() -> None:
            try:
                log = self.config.logging
                setup_logging(level=log.level, fmt=log.format, file=log.file, max_bytes=log.max_bytes, backup_count=log.backup_count, console=False)
                self.engine.start()
                self._post(self._engine_ready)
            except Exception as exc:
                self._post(self._fatal_startup, exc, traceback.format_exc())
            finally:
                with self.worker_lock:
                    self.workers.discard(threading.current_thread())

        thread = threading.Thread(target=work, name="v1-startup", daemon=True)
        with self.worker_lock:
            self.workers.add(thread)
        thread.start()

    def _engine_ready(self) -> None:
        self.status_text.set("●  Library ready")
        self.progress_text.set("Ready")
        self.refresh_all()
        if not self.config.library.roots:
            self.root.after(250, self._first_run_prompt)

    def _fatal_startup(self, exc: Exception, details: str) -> None:
        self.status_text.set("●  Startup failed")
        messagebox.showerror("File Indexer could not start", f"{exc}\n\nDetails were written to the engine log.")
        _LOG.error("desktop startup failed\n%s", details)

    def _post(self, callback: Callable[..., Any], *args: Any) -> None:
        self.events.put((callback, args))

    def _drain_events(self) -> None:
        try:
            while True:
                callback, args = self.events.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        if self.close_pending and not self._work_running():
            self._finish_close()
            return
        self.root.after(60, self._drain_events)

    def _run_background(
        self,
        name: str,
        work: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
        *,
        long_job: bool = False,
    ) -> bool:
        if long_job and self._job_running():
            messagebox.showinfo("A job is already running", "Wait for the current scan or analyzer to finish, or cancel it first.")
            return False

        def runner() -> None:
            try:
                result = work()
                if done is not None:
                    self._post(done, result)
            except Exception as exc:
                self._post(self._background_failed, name, exc, traceback.format_exc(), long_job)
            finally:
                if long_job:
                    self._post(self._long_job_finished)
                with self.worker_lock:
                    self.workers.discard(threading.current_thread())

        thread = threading.Thread(target=runner, name=f"v1-{name}", daemon=True)
        with self.worker_lock:
            self.workers.add(thread)
        if long_job:
            self.long_job = thread
            self.cancel_button.configure(state="normal")
            self.status_text.set(f"●  {name.capitalize()} running")
        thread.start()
        return True

    def _background_failed(self, name: str, exc: Exception, details: str, long_job: bool) -> None:
        self.progress_text.set(f"{name.capitalize()} failed")
        self.progress.configure(mode="determinate", value=0)
        _LOG.error("%s failed\n%s", name, details)
        if not self.close_pending:
            messagebox.showerror(f"{name.capitalize()} failed", str(exc))

    def _long_job_finished(self) -> None:
        self.cancel_button.configure(state="disabled")
        self.cancel_token = None
        self.status_text.set("●  Library ready")
        self.long_job = None

    def _job_running(self) -> bool:
        return self.long_job is not None and self.long_job.is_alive()

    def _work_running(self) -> bool:
        """Whether any engine operation still owns a live worker thread."""
        with self.worker_lock:
            return bool(self.workers)

    # ── navigation and refresh ─────────────────────────────────────────────

    def show_page(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill=BOTH, expand=True)
        for key, button in self.nav_buttons.items():
            button.configure(style="Selected.Nav.TButton" if key == name else "Nav.TButton")
        if name == "activity" and self.engine.started:
            self.refresh_activity()
        elif name == "plugins" and self.engine.started:
            self.refresh_plugins()

    def refresh_all(self) -> None:
        if not self.engine.started:
            return
        self.run_search(reset=True)
        self._run_background("statistics", self.engine.stats, self._show_stats)

    def _show_stats(self, stats: dict[str, Any]) -> None:
        types = stats.get("assets_by_type", {})
        if not isinstance(types, dict):
            types = {}
        total = sum(int(value) for value in types.values())
        self.card_values["assets"].set(f"{total:,}")
        self.card_values["images"].set(f"{int(types.get('image', 0)):,}")
        self.card_values["videos"].set(f"{int(types.get('video', 0)):,}")
        self.card_values["documents"].set(f"{int(types.get('document', 0)):,}")

    # ── search and asset detail ────────────────────────────────────────────

    def run_search(self, *, reset: bool) -> None:
        if not self.engine.started:
            return
        if reset:
            self.search_offset = 0
        raw = self.search_text.get().strip()
        media_type = self.type_filter.get()
        if media_type != "all":
            raw = f"{raw} type:{media_type}".strip()
        query = parse_query(raw, limit=self.page_size, offset=self.search_offset)
        self.search_generation += 1
        generation = self.search_generation
        self.result_count.set("Searching…")

        def done(result: dict[str, Any]) -> None:
            if generation == self.search_generation:
                self._show_results(result)

        self._run_background("search", lambda: self.engine.search(query, with_facets=False), done)

    def _show_results(self, result: dict[str, Any]) -> None:
        self.results.delete(*self.results.get_children())
        self.result_rows.clear()
        self.current_total = int(result.get("total", 0))
        for row in result.get("items", []):
            width, height = row.get("width"), row.get("height")
            dimensions = f"{width} × {height}" if width and height else "—"
            captured = str(row.get("captured_at") or "—").replace("T", " ")[:19]
            iid = str(row["id"])
            self.result_rows[iid] = row
            self.results.insert("", END, iid=iid, values=(row.get("filename") or "(unnamed)", row.get("media_type") or "other", format_bytes(row.get("size_bytes")), captured, dimensions))
        first = self.search_offset + 1 if self.current_total else 0
        last = min(self.search_offset + self.page_size, self.current_total)
        self.result_count.set(f"{self.current_total:,} items  ·  showing {first:,}–{last:,}")
        page_number = self.search_offset // self.page_size + 1
        page_total = max(1, (self.current_total + self.page_size - 1) // self.page_size)
        self.page_text.set(f"Page {page_number:,} of {page_total:,}")
        self.prev_button.configure(state="normal" if self.search_offset else "disabled")
        self.next_button.configure(state="normal" if self.search_offset + self.page_size < self.current_total else "disabled")

    def change_page(self, direction: int) -> None:
        candidate = self.search_offset + direction * self.page_size
        self.search_offset = max(0, min(candidate, max(0, self.current_total - 1)))
        self.run_search(reset=False)

    def _selected_row(self) -> dict[str, Any] | None:
        selected = self.results.selection()
        return self.result_rows.get(selected[0]) if selected else None

    def _result_selected(self, _: object = None) -> None:
        row = self._selected_row()
        if row is None:
            return
        asset_id = int(row["id"])
        self.preview.configure(text="Loading preview…", image="")
        self._run_background("details", lambda: self._load_asset_detail(asset_id), self._show_asset_detail)

    def _load_asset_detail(self, asset_id: int) -> tuple[int, dict[str, Any], Path | None]:
        detail = self.engine.asset(asset_id)
        derivatives = detail.get("derivatives") or []
        thumbs = [item for item in derivatives if item.get("kind") == "thumb"]
        chosen = min(thumbs, key=lambda item: abs(int(item.get("variant", 0)) - 512)) if thumbs else None
        path = safe_derivative_path(self.config, chosen.get("rel_path") if chosen else None)
        return asset_id, detail, path

    def _show_asset_detail(self, payload: tuple[int, dict[str, Any], Path | None]) -> None:
        asset_id, detail, thumbnail = payload
        selected = self._selected_row()
        if selected is None or int(selected["id"]) != asset_id:
            return
        if thumbnail is not None:
            try:
                with Image.open(thumbnail) as image:
                    image.thumbnail((380, 245), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGB", (380, 245), PANEL)
                    fitted = ImageOps.contain(image.convert("RGB"), (380, 245))
                    canvas.paste(fitted, ((380 - fitted.width) // 2, (245 - fitted.height) // 2))
                    self.thumbnail_image = ImageTk.PhotoImage(canvas)
                self.preview.configure(image=self.thumbnail_image, text="")
            except Exception:
                self.thumbnail_image = None
                self.preview.configure(image="", text="Preview unavailable")
        else:
            self.thumbnail_image = None
            self.preview.configure(image="", text="No preview available")

        asset = detail.get("asset") or {}
        technical = detail.get("technical_metadata") or {}
        files = detail.get("files") or []
        annotations = [item for item in detail.get("annotations") or [] if item.get("superseded_by") is None]
        lines = [f"ASSET #{asset_id}", ""]
        for label, value in (("Type", asset.get("media_type")), ("MIME", asset.get("mime_type")), ("Size", format_bytes(asset.get("size_bytes"))), ("Captured", asset.get("captured_at")), ("Hash", asset.get("content_hash"))):
            lines.append(f"{label}: {display_value(value)}")
        if technical:
            lines.extend(["", "TECHNICAL"])
            for key in ("width", "height", "duration_s", "frame_rate", "video_codec", "audio_codec", "camera_make", "camera_model", "lens_model", "iso", "f_number", "page_count", "word_count", "extractor"):
                if technical.get(key) is not None:
                    lines.append(f"{key.replace('_', ' ').title()}: {display_value(technical[key])}")
        if files:
            lines.extend(["", "FILES"])
            lines.extend(str(item.get("path")) for item in files)
        if annotations:
            lines.extend(["", "LABELS"])
            for item in annotations:
                confidence = f" ({float(item['confidence']):.0%})" if item.get("confidence") is not None else ""
                lines.append(f"{item.get('namespace')}: {item.get('label')}{confidence}")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", END)
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.configure(state="disabled")

    def _result_menu(self, event: Any) -> None:
        item = self.results.identify_row(event.y)
        if not item:
            return
        self.results.selection_set(item)
        menu = Menu(self.root, tearoff=False)
        menu.add_command(label="Open", command=self.open_selected)
        menu.add_command(label="Show in folder", command=self.reveal_selected)
        menu.add_separator()
        menu.add_command(label="Add label…", command=self.add_label)
        menu.tk_popup(event.x_root, event.y_root)

    def open_selected(self) -> None:
        row = self._selected_row()
        if row and row.get("path"):
            self._open_path(Path(str(row["path"])))

    def reveal_selected(self) -> None:
        row = self._selected_row()
        if not row or not row.get("path"):
            return
        path = Path(str(row["path"]))
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                self._open_path(path.parent)
        except OSError as exc:
            messagebox.showerror("Could not show the file", str(exc))

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open", str(exc))

    def add_label(self) -> None:
        row = self._selected_row()
        if row is None:
            messagebox.showinfo("Select an item", "Choose an item in the library first.")
            return
        entered = simpledialog.askstring("Add a label", "Label (for example: favorite or project-alpha):", parent=self.root)
        if not entered:
            return
        label = entered.strip()
        if not label:
            return

        def work() -> int:
            annotation_id = cast(
                int,
                self.engine.repos.annotations.add_user_annotation(
                    int(row["id"]), "user.label", label
                ),
            )
            self.engine.pipeline().reindex_asset(int(row["id"]))
            return annotation_id

        self._run_background("label", work, lambda _: self._result_selected())

    # ── scanning ───────────────────────────────────────────────────────────

    def _first_run_prompt(self) -> None:
        if messagebox.askyesno("Welcome to File Indexer V1", "Choose your first media folder now?\n\nOriginal files are always read-only."):
            self.choose_scan_folder()

    def choose_scan_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose a folder to index", mustexist=True)
        if not selected:
            return
        path = Path(selected).resolve()
        if path not in self.config.library.roots:
            self.config.library.roots.append(path)
            self.roots_list.insert(END, str(path))
            save_desktop_config(self.config)
        self._start_scan([path])

    def scan_configured(self) -> None:
        if not self.config.library.roots:
            self.choose_scan_folder()
            return
        self._start_scan(list(self.config.library.roots))

    def _start_scan(self, roots: list[Path]) -> None:
        token = CancelToken()
        self.cancel_token = token
        # Tk variables are bound to the UI interpreter and must only be read
        # on its owning thread.  Capture them before dispatching the worker.
        rehash = self.rehash_var.get()
        generate_derivatives = self.derive_var.get()
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_text.set("Preparing scan…")

        def progress(event: ProgressEvent) -> None:
            self._post(self._show_progress, event)

        def work() -> Any:
            return self.engine.scan(
                roots,
                resume=True,
                rehash=rehash,
                generate_derivatives=generate_derivatives,
                progress=progress,
                cancel=token,
            )

        def done(results: Any) -> None:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            new_count = sum(item.files_new for item in results)
            updated = sum(item.files_updated for item in results)
            failed = sum(item.files_failed for item in results)
            self.progress_text.set(f"Scan complete · {new_count:,} new · {updated:,} updated · {failed:,} failed")
            self.rehash_var.set(False)
            self.refresh_all()
            self.refresh_activity()

        self._run_background("scan", work, done, long_job=True)

    def _show_progress(self, event: ProgressEvent) -> None:
        if event.fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=event.fraction * 100)
        detail = f" · {event.done:,}/{event.total:,}" if event.total else (f" · {event.done:,}" if event.done else "")
        self.progress_text.set(f"{event.stage.title()}{detail} · {event.message}".rstrip(" ·"))

    def cancel_job(self) -> None:
        if self.cancel_token is not None:
            self.cancel_token.cancel("cancelled in File Indexer V1")
            self.progress_text.set("Stopping safely…")
            self.cancel_button.configure(state="disabled")

    # ── activity and plugins ───────────────────────────────────────────────

    def refresh_activity(self) -> None:
        def work() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return self.engine.scans(100), self.engine.errors(200)
        self._run_background("activity", work, self._show_activity)

    def _show_activity(self, payload: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> None:
        scans, errors = payload
        self.scans_tree.delete(*self.scans_tree.get_children())
        for item in scans:
            self.scans_tree.insert("", END, values=(item.get("id"), item.get("root_path"), item.get("state"), item.get("files_seen", 0), item.get("files_new", 0), item.get("errors", 0), str(item.get("started_at") or "").replace("T", " ")[:19]))
        self.errors_tree.delete(*self.errors_tree.get_children())
        for item in errors:
            iid = str(item.get("id"))
            self.errors_tree.insert("", END, iid=iid, values=(str(item.get("occurred_at") or "").replace("T", " ")[:19], item.get("scope"), item.get("kind"), item.get("ref_text") or item.get("ref_id"), item.get("message")))
            self.errors_tree.set(iid, "message", str(item.get("message") or ""))
        self._activity_errors = {str(item.get("id")): item for item in errors}

    def _show_error(self, _: object = None) -> None:
        selected = self.errors_tree.selection()
        if not selected:
            return
        item = getattr(self, "_activity_errors", {}).get(selected[0], {})
        dialog = Toplevel(self.root)
        dialog.title(f"Error #{item.get('id', '')}")
        dialog.geometry("760x460")
        dialog.configure(bg=BG)
        text = Text(dialog, bg=PANEL, fg=TEXT, insertbackground=TEXT, wrap="word", font=("Consolas", 9), padx=12, pady=12)
        text.pack(fill=BOTH, expand=True, padx=10, pady=10)
        text.insert("1.0", json.dumps(item, indent=2, ensure_ascii=False))
        text.configure(state="disabled")

    def refresh_plugins(self) -> None:
        self._run_background("analyzers", lambda: self.engine.plugins.describe(), self._show_plugins)

    def _show_plugins(self, plugins: list[dict[str, object]]) -> None:
        self.plugins_tree.delete(*self.plugins_tree.get_children())
        for plugin in plugins:
            task_counts = plugin.get("tasks") or {}
            state = ", ".join(f"{key}: {value}" for key, value in task_counts.items()) if isinstance(task_counts, dict) else ""
            self.plugins_tree.insert("", END, iid=str(plugin["plugin_id"]), values=(plugin.get("plugin_id"), plugin.get("version"), plugin.get("transport"), "Yes" if plugin.get("enabled") else "No", state or "—", plugin.get("load_error") or plugin.get("description") or ""))

    def run_selected_plugin(self) -> None:
        selected = self.plugins_tree.selection()
        if not selected:
            messagebox.showinfo("Select an analyzer", "Choose an analyzer in the list first.")
            return
        plugin_id = selected[0]
        plugin = self.engine.plugins.get(plugin_id)
        if plugin is None or not plugin.enabled:
            messagebox.showwarning("Analyzer is disabled", "Enable this analyzer in config.yaml before running it. Capability-gated analyzers are never enabled silently.")
            return
        token = CancelToken()
        self.cancel_token = token
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_text.set(f"Running {plugin_id}…")

        def progress(event: ProgressEvent) -> None:
            self._post(self._show_progress, event)

        def done(result: Any) -> None:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            self.progress_text.set(f"Analyzer complete · {result.completed:,} done · {result.failed:,} failed")
            self.refresh_all()
            self.refresh_plugins()

        # PluginRunner currently owns the engine-wide token. V1 still keeps a
        # GUI token for consistent state; engine.cancel handles this job.
        self._run_background(
            "analysis",
            lambda: self.engine.backfill([plugin_id], progress=progress, cancel=token),
            done,
            long_job=True,
        )

    # ── settings and diagnostics ───────────────────────────────────────────

    def add_root(self) -> None:
        selected = filedialog.askdirectory(title="Add a library folder", mustexist=True)
        if selected and selected not in self.roots_list.get(0, END):
            self.roots_list.insert(END, str(Path(selected).resolve()))

    def remove_root(self) -> None:
        selected = list(self.roots_list.curselection())  # type: ignore[no-untyped-call]
        for index in reversed(selected):
            self.roots_list.delete(index)

    def choose_database(self) -> None:
        selected = filedialog.asksaveasfilename(title="Choose index database", defaultextension=".db", filetypes=(("SQLite database", "*.db"), ("All files", "*.*")), initialfile="library.db")
        if selected:
            self.db_path_var.set(str(Path(selected).resolve()))

    def choose_cache(self) -> None:
        selected = filedialog.askdirectory(title="Choose preview cache location")
        if selected:
            self.cache_path_var.set(str(Path(selected).resolve()))

    def save_settings(self) -> None:
        roots = [Path(item).resolve() for item in self.roots_list.get(0, END)]
        db_path = Path(self.db_path_var.get()).expanduser().resolve()
        cache_path = Path(self.cache_path_var.get()).expanduser().resolve()
        storage_changed = db_path != self.config.storage.db_path or cache_path != self.config.storage.derivatives_path
        for root in roots:
            try:
                cache_path.relative_to(root)
            except ValueError:
                continue
            messagebox.showerror("Invalid preview cache", "The preview cache cannot be inside a library folder, or it would index its own thumbnails.")
            return
        if storage_changed and self._work_running():
            messagebox.showwarning("Job in progress", "Wait for the current job to finish before changing index storage.")
            return
        self.config.library.roots = roots
        self.config.storage.db_path = db_path
        self.config.storage.derivatives_path = cache_path
        try:
            path = save_desktop_config(self.config)
        except OSError as exc:
            messagebox.showerror("Could not save settings", str(exc))
            return
        if storage_changed:
            self.engine.close()
            self.engine = MediaEngine(self.config)
            self._start_engine()
        messagebox.showinfo("Settings saved", f"Saved to:\n{path}")

    def run_integrity_check(self) -> None:
        self.progress_text.set("Checking the database…")

        def done(result: list[str]) -> None:
            healthy = result == ["ok"]
            self.progress_text.set("Database integrity: OK" if healthy else "Database integrity issue detected")
            messagebox.showinfo("Integrity check", "Database is healthy." if healthy else "\n".join(result))

        self._run_background("integrity check", self.engine.integrity_check, done)

    # ── shutdown ───────────────────────────────────────────────────────────

    def on_close(self) -> None:
        if self._job_running():
            if not messagebox.askyesno("A job is still running", "Stop it safely and close File Indexer when it has finished its current item?"):
                return
            self.close_pending = True
            self.cancel_job()
            self.status_text.set("●  Closing safely…")
            return
        if self._work_running():
            self.close_pending = True
            self.status_text.set("●  Finishing current read…")
            return
        self._finish_close()

    def _finish_close(self) -> None:
        try:
            self.engine.close()
        finally:
            # pythonw keeps no parent terminal around to own logging.  Close
            # the rotating file explicitly so portable drives and temporary
            # data folders can be removed immediately after the window exits.
            logging.shutdown()
            self.root.destroy()


def _show_uncaught(title: str, message: str) -> None:
    """Display startup failures even when launched through pythonw.exe."""
    try:
        root = Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def main() -> None:
    """Start the no-console V1 desktop application."""
    try:
        root = Tk()
        FileIndexerV1(root)
        root.mainloop()
    except Exception as exc:
        _show_uncaught("File Indexer V1 could not start", f"{type(exc).__name__}: {exc}")
        raise
