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
from ..search import parse_query
from ..util import human_bytes, setup_logging
from .appearance import (
    THEMES,
    GradientHeader,
    PlaceholderEntry,
    UISettings,
    load_ui_settings,
    mix_color,
    save_ui_settings,
)
from .config_store import APP_NAME, load_desktop_config, save_desktop_config
from .tutorials import HelpCenterDialog, TutorialDialog

_LOG = logging.getLogger(__name__)

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
        self.ui_settings = load_ui_settings(self.config)
        self.theme = THEMES[self.ui_settings.theme]
        self.scale = self.ui_settings.text_scale / 100.0
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
        self.current_page = "library"
        self.compact_layout = False
        self.resize_job: str | None = None
        self.stats_cards: list[ttk.Frame] = []
        self.custom_text_widgets: list[Any] = []
        self.nav_labels = {
            "library": ("Library", "▦"),
            "activity": ("Activity", "◷"),
            "plugins": ("Analyzers", "◇"),
            "settings": ("Settings", "⚙"),
        }

        self.progress_text = StringVar(value="Ready")
        self.search_text = StringVar()
        self.activity_search_text = StringVar()
        self.plugin_search_text = StringVar()
        self.type_filter = StringVar(value="all")
        self.result_count = StringVar(value="0 items")
        self.page_text = StringVar(value="Page 1")
        self.derive_var = BooleanVar(value=True)
        self.rehash_var = BooleanVar(value=False)
        self.db_path_var = StringVar(value=str(self.config.storage.db_path))
        self.cache_path_var = StringVar(value=str(self.config.storage.derivatives_path))
        self.theme_var = StringVar(value=self.ui_settings.theme)
        self.density_var = StringVar(value=self.ui_settings.density)
        self.text_scale_var = StringVar(value=str(self.ui_settings.text_scale))
        self.reduced_motion_var = BooleanVar(value=self.ui_settings.reduced_motion)

        self._configure_window()
        self._configure_style()
        self._build_shell()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Configure>", self._schedule_responsive_layout)
        self.root.bind("<Control-k>", lambda _: self.focus_search())
        self.root.bind("<Control-l>", lambda _: self.show_page("library"))
        self.root.bind("<Control-comma>", lambda _: self.show_page("settings"))
        self.root.bind("<F1>", lambda _: self.open_help())
        self.root.bind("<Escape>", lambda _: self.clear_active_search())
        self.root.after(60, self._drain_events)
        self._start_engine()

    # ── window and theme ──────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} — Special Edition")
        self.root.geometry("1280x800")
        self.root.minsize(900, 620)
        self.root.configure(bg=self.theme.bg)

    def _configure_style(self) -> None:
        t = self.theme
        def size(value: int) -> int:
            return max(8, round(value * self.scale))
        density = {"Compact": 27, "Comfortable": 34, "Cozy": 40}[self.ui_settings.density]
        button_y = {"Compact": 6, "Comfortable": 9, "Cozy": 12}[self.ui_settings.density]
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure(".", background=t.bg, foreground=t.text, font=("Segoe UI", size(10)))
        self.style.configure("TFrame", background=t.bg)
        self.style.configure("Panel.TFrame", background=t.panel)
        self.style.configure("Panel2.TFrame", background=t.panel_alt)
        self.style.configure("Raised.TFrame", background=t.raised)
        self.style.configure("TLabel", background=t.bg, foreground=t.text)
        self.style.configure("Muted.TLabel", background=t.bg, foreground=t.muted)
        self.style.configure("Panel.TLabel", background=t.panel, foreground=t.text)
        self.style.configure("PanelMuted.TLabel", background=t.panel, foreground=t.muted)
        self.style.configure("Raised.TLabel", background=t.raised, foreground=t.text)
        self.style.configure("Title.TLabel", font=("Segoe UI Semibold", size(23)), foreground=t.text)
        self.style.configure("Subtitle.TLabel", font=("Segoe UI Semibold", size(16)), foreground=t.text)
        self.style.configure("TutorialTitle.TLabel", font=("Segoe UI Semibold", size(25)), foreground=t.text)
        self.style.configure("Eyebrow.TLabel", font=("Segoe UI Semibold", size(9)), foreground=t.accent)
        self.style.configure("Body.TLabel", font=("Segoe UI", size(11)), foreground=t.text)
        self.style.configure("CardValue.TLabel", background=t.panel, font=("Segoe UI Semibold", size(23)), foreground=t.text)
        self.style.configure("CardLabel.TLabel", background=t.panel, foreground=t.muted)
        self.style.configure("CardAccent.TLabel", background=t.panel, foreground=t.accent, font=("Segoe UI Semibold", size(9)))
        self.style.configure("TButton", background=t.panel_alt, foreground=t.text, padding=(13, button_y), borderwidth=0)
        self.style.map("TButton", background=[("active", t.border), ("pressed", t.selection)])
        self.style.configure("Accent.TButton", background=t.accent, foreground="#ffffff", padding=(15, button_y + 1))
        self.style.map("Accent.TButton", background=[("active", t.accent_hover), ("pressed", t.accent_hover)])
        self.style.configure("Quiet.TButton", background=t.bg, foreground=t.muted, padding=(10, button_y))
        self.style.configure("Chip.TButton", background=t.raised, foreground=t.text, padding=(11, 5), borderwidth=1, bordercolor=t.border)
        self.style.map("Chip.TButton", background=[("active", t.selection)], foreground=[("active", t.text)])
        self.style.configure("Nav.TButton", anchor="w", background=t.bg, foreground=t.muted, padding=(17, 12))
        self.style.map("Nav.TButton", background=[("active", t.panel_alt)], foreground=[("active", t.text)])
        self.style.configure("Selected.Nav.TButton", anchor="w", background=t.panel_alt, foreground=t.accent, padding=(17, 12))
        self.style.configure("CompactNav.TButton", anchor="center", background=t.bg, foreground=t.muted, padding=(6, 13))
        self.style.map("CompactNav.TButton", background=[("active", t.panel_alt)], foreground=[("active", t.text)])
        self.style.configure("Selected.CompactNav.TButton", anchor="center", background=t.panel_alt, foreground=t.accent, padding=(6, 13))
        self.style.configure("Treeview", background=t.panel, fieldbackground=t.panel, foreground=t.text, rowheight=round(density * self.scale), borderwidth=0)
        self.style.map("Treeview", background=[("selected", t.selection)], foreground=[("selected", t.text)])
        self.style.configure("Treeview.Heading", background=t.panel_alt, foreground=t.muted, relief="flat", padding=8)
        self.style.map("Treeview.Heading", background=[("active", t.border)])
        self.style.configure("TEntry", fieldbackground=t.panel, foreground=t.text, insertcolor=t.text, bordercolor=t.border, lightcolor=t.border, darkcolor=t.border, padding=10)
        self.style.configure("Placeholder.TEntry", fieldbackground=t.panel, foreground=t.muted, insertcolor=t.muted, bordercolor=t.border, padding=10)
        self.style.configure("TCombobox", fieldbackground=t.panel, background=t.panel, foreground=t.text, arrowcolor=t.text, bordercolor=t.border, padding=7)
        self.style.map("TCombobox", fieldbackground=[("readonly", t.panel)], foreground=[("readonly", t.text)])
        self.style.configure("TNotebook", background=t.bg, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=t.panel_alt, foreground=t.muted, padding=(16, 9))
        self.style.map("TNotebook.Tab", background=[("selected", t.panel)], foreground=[("selected", t.text)])
        self.style.configure("Horizontal.TProgressbar", background=t.secondary, troughcolor=t.panel_alt, borderwidth=0)
        self.style.configure("TCheckbutton", background=t.bg, foreground=t.text)
        self.style.map("TCheckbutton", background=[("active", t.bg)])
        self.style.configure("TScale", background=t.bg, troughcolor=t.panel_alt)
        self.style.configure("TSeparator", background=t.border)

    def _build_shell(self) -> None:
        self.header = GradientHeader(
            self.root,
            self.theme,
            scale=self.scale,
            reduced_motion=self.ui_settings.reduced_motion,
            on_help=self.open_help,
            on_customize=lambda: self.show_page("settings"),
        )
        self.header.pack(fill="x")

        self.body = ttk.Frame(self.root)
        self.body.pack(fill=BOTH, expand=True)
        self.sidebar = ttk.Frame(self.body, width=186, padding=(10, 20))
        self.sidebar.pack(side=LEFT, fill="y")
        self.sidebar.pack_propagate(False)
        for key, (label, _) in self.nav_labels.items():
            button = ttk.Button(
                self.sidebar,
                text=f"  {label}",
                style="Nav.TButton",
                command=partial(self.show_page, key),
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button
        self.engine_version_label = ttk.Label(self.sidebar, text=f"Engine {__version__}", style="Muted.TLabel")
        self.engine_version_label.pack(side="bottom", pady=10)

        self.content = ttk.Frame(self.body, padding=(4, 8, 18, 8))
        self.content.pack(side=LEFT, fill=BOTH, expand=True)
        self.pages["library"] = self._build_library_page(self.content)
        self.pages["activity"] = self._build_activity_page(self.content)
        self.pages["plugins"] = self._build_plugins_page(self.content)
        self.pages["settings"] = self._build_settings_page(self.content)

        self.footer = ttk.Frame(self.root, style="Panel.TFrame", padding=(18, 9))
        # Pack the footer before the expanding body in the geometry order so a
        # content-heavy page can never push job status below the window edge.
        self.footer.pack(side="bottom", fill="x", before=self.body)
        self.progress = ttk.Progressbar(self.footer, mode="determinate", length=240)
        self.progress.pack(side=LEFT, padx=(0, 12))
        ttk.Label(self.footer, textvariable=self.progress_text, style="PanelMuted.TLabel").pack(side=LEFT)
        self.cancel_button = ttk.Button(self.footer, text="Cancel", command=self.cancel_job, state="disabled")
        self.cancel_button.pack(side=RIGHT)
        self.show_page("library")

    # ── page construction ─────────────────────────────────────────────────

    def _page(self, parent: ttk.Frame, title: str, subtitle: str) -> ttk.Frame:
        page = ttk.Frame(parent)
        ttk.Label(page, text=title, style="Title.TLabel").pack(anchor="w", pady=(10, 1))
        ttk.Label(page, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(0, 14))
        return page

    def _build_library_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Your library", "Find anything by filename, metadata, document text, or analyzer label.")
        self.library_toolbar = ttk.Frame(page)
        self.library_toolbar.pack(fill="x", pady=(0, 7))
        self.library_toolbar.columnconfigure(0, weight=1)
        self.search_entry = PlaceholderEntry(
            self.library_toolbar,
            self.search_text,
            "Search names, text, places, and labels…   (Ctrl+K)",
        )
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<Return>", lambda _: self.run_search(reset=True))
        self.search_clear_button = ttk.Button(self.library_toolbar, text="Clear", style="Quiet.TButton", command=self.clear_search)
        self.search_clear_button.grid(row=0, column=1, padx=(6, 0))
        self.type_selector = ttk.Combobox(
            self.library_toolbar,
            textvariable=self.type_filter,
            values=("all", "image", "video", "audio", "document", "other"),
            state="readonly",
            width=11,
        )
        self.type_selector.grid(row=0, column=2, padx=7)
        self.type_selector.bind("<<ComboboxSelected>>", lambda _: self.run_search(reset=True))
        self.search_button = ttk.Button(self.library_toolbar, text="Search", command=lambda: self.run_search(reset=True))
        self.search_button.grid(row=0, column=3)
        self.add_scan_button = ttk.Button(self.library_toolbar, text="Add & scan folder", style="Accent.TButton", command=self.choose_scan_folder)
        self.add_scan_button.grid(row=0, column=4, padx=(7, 0))

        self.quick_filters = ttk.Frame(page)
        self.quick_filters.pack(fill="x", pady=(0, 12))
        ttk.Label(self.quick_filters, text="Try", style="Muted.TLabel").pack(side=LEFT, padx=(1, 8))
        for label, query in (
            ("Recent", "after:2025 sort:-captured"),
            ("Images", "type:image"),
            ("Videos", "type:video"),
            ("With location", "has:gps"),
            ("Favorites", "user.label:favorite"),
        ):
            ttk.Button(
                self.quick_filters,
                text=label,
                style="Chip.TButton",
                command=partial(self.apply_quick_search, query),
            ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(self.quick_filters, text="Search tips", style="Quiet.TButton", command=self.open_help).pack(side=RIGHT)

        self.cards_container = ttk.Frame(page)
        self.cards_container.pack(fill="x", pady=(0, 12))
        self.card_values: dict[str, StringVar] = {}
        for index, (key, label, eyebrow) in enumerate((
            ("assets", "Indexed items", "LIBRARY"),
            ("images", "Images", "VISUAL"),
            ("videos", "Videos", "MOTION"),
            ("documents", "Documents", "TEXT"),
        )):
            card = ttk.Frame(self.cards_container, style="Panel.TFrame", padding=(16, 11))
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 8) if index < 3 else 0)
            self.cards_container.columnconfigure(index, weight=1, uniform="stats")
            self.stats_cards.append(card)
            value = StringVar(value="—")
            self.card_values[key] = value
            ttk.Label(card, text=eyebrow, style="CardAccent.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=value, style="CardValue.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="CardLabel.TLabel").pack(anchor="w")

        self.library_paned = ttk.Panedwindow(page, orient="horizontal")
        self.library_paned.pack(fill=BOTH, expand=True)
        self.results_panel = ttk.Frame(self.library_paned, style="Panel.TFrame", padding=10)
        self.detail_panel = ttk.Frame(self.library_paned, style="Panel.TFrame", padding=13)
        self.library_paned.add(self.results_panel, weight=4)
        self.library_paned.add(self.detail_panel, weight=2)

        top = ttk.Frame(self.results_panel, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, textvariable=self.result_count, style="PanelMuted.TLabel").pack(side=LEFT)
        self.results = ttk.Treeview(self.results_panel, columns=("name", "type", "size", "date", "dimensions"), show="headings", selectmode="browse")
        for column, label, width, anchor in (
            ("name", "Name", 285, "w"), ("type", "Type", 80, "center"),
            ("size", "Size", 85, "e"), ("date", "Captured", 145, "w"),
            ("dimensions", "Dimensions", 100, "center"),
        ):
            self.results.heading(column, text=label)
            tree_anchor = cast(Literal["w", "center", "e"], anchor)
            self.results.column(column, width=width, minwidth=55, anchor=tree_anchor)
        scroll = ttk.Scrollbar(self.results_panel, orient=VERTICAL, command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        self.results.pack(fill=BOTH, expand=True)
        self.results.bind("<<TreeviewSelect>>", self._result_selected)
        self.results.bind("<Double-1>", lambda _: self.open_selected())
        self.results.bind("<Button-3>", self._result_menu)
        pager = ttk.Frame(self.results_panel, style="Panel.TFrame")
        pager.pack(fill="x", pady=(8, 0))
        self.prev_button = ttk.Button(pager, text="← Previous", command=lambda: self.change_page(-1), state="disabled")
        self.prev_button.pack(side=LEFT)
        ttk.Label(pager, textvariable=self.page_text, style="PanelMuted.TLabel").pack(side=LEFT, padx=12)
        self.next_button = ttk.Button(pager, text="Next →", command=lambda: self.change_page(1), state="disabled")
        self.next_button.pack(side=LEFT)
        ttk.Button(pager, text="Refresh", command=self.refresh_all).pack(side=RIGHT)

        ttk.Label(self.detail_panel, text="INSPECTOR", style="CardAccent.TLabel").pack(anchor="w", pady=(0, 5))
        self.preview = ttk.Label(
            self.detail_panel,
            text="◇\n\nSelect an item to see its preview, metadata, labels, and every known file location.",
            style="PanelMuted.TLabel",
            anchor="center",
            justify="center",
            wraplength=340,
            font=("Segoe UI", max(10, round(11 * self.scale))),
        )
        self.preview.pack(fill="x", pady=(10, 12))
        detail_actions = ttk.Frame(self.detail_panel, style="Panel.TFrame")
        detail_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(detail_actions, text="Open", command=self.open_selected).pack(side=LEFT)
        ttk.Button(detail_actions, text="Show in folder", command=self.reveal_selected).pack(side=LEFT, padx=5)
        ttk.Button(detail_actions, text="Add label", command=self.add_label).pack(side=LEFT)
        self.detail = Text(self.detail_panel, bg=self.theme.panel, fg=self.theme.text, insertbackground=self.theme.text, relief="flat", wrap="word", font=("Cascadia Mono", max(8, round(9 * self.scale))), padx=4, pady=4, state="disabled")
        self.detail.pack(fill=BOTH, expand=True)
        self.custom_text_widgets.append(self.detail)
        return page

    def _build_activity_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Activity", "Scan history and actionable extraction errors.")
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Scan all configured folders", style="Accent.TButton", command=self.scan_configured).pack(side=LEFT)
        ttk.Button(actions, text="Refresh", command=self.refresh_activity).pack(side=LEFT, padx=8)
        self.activity_search = PlaceholderEntry(actions, self.activity_search_text, "Filter scans and errors…", width=34)
        self.activity_search.pack(side=RIGHT)
        self.activity_search.bind("<KeyRelease>", lambda _: self.filter_activity())
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
        page = self._page(parent, "Analyzer shop", "Turn on built-ins, connect local APIs, and run optional AI without changing the core.")
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Refresh", command=self.refresh_plugins).pack(side=LEFT)
        ttk.Button(actions, text="Run selected", style="Accent.TButton", command=self.run_selected_plugin).pack(side=LEFT, padx=8)
        ttk.Button(actions, text="Enable / Disable", command=self.toggle_selected_plugin).pack(side=LEFT)
        ttk.Button(actions, text="Add API…", command=self.add_plugin_api).pack(side=LEFT, padx=8)
        ttk.Button(actions, text="LM Studio…", command=self.configure_lm_studio).pack(side=LEFT)
        self.plugin_search = PlaceholderEntry(actions, self.plugin_search_text, "Search analyzers…", width=30)
        self.plugin_search.pack(side=RIGHT)
        self.plugin_search.bind("<KeyRelease>", lambda _: self.filter_plugins())
        self.plugins_tree = ttk.Treeview(page, columns=("id", "version", "kind", "enabled", "state", "description"), show="headings")
        for col, label, width in (("id", "Analyzer", 210), ("version", "Version", 80), ("kind", "Kind", 110), ("enabled", "Enabled", 75), ("state", "Tasks", 150), ("description", "Description", 400)):
            self.plugins_tree.heading(col, text=label)
            self.plugins_tree.column(col, width=width, anchor="w")
        self.plugins_tree.pack(fill=BOTH, expand=True)
        return page

    def _build_settings_page(self, parent: ttk.Frame) -> ttk.Frame:
        page = self._page(parent, "Settings", "Customize the workspace and decide exactly what V1 indexes.")
        notebook = ttk.Notebook(page)
        notebook.pack(fill=BOTH, expand=True)
        appearance_tab = ttk.Frame(notebook, padding=18)
        storage_tab = ttk.Frame(notebook, padding=18)
        scan_tab = ttk.Frame(notebook, padding=18)
        notebook.add(appearance_tab, text="Appearance")
        notebook.add(storage_tab, text="Library & storage")
        notebook.add(scan_tab, text="Scanning & maintenance")

        appearance = ttk.Frame(appearance_tab, style="Panel.TFrame", padding=20)
        appearance.pack(fill="x")
        ttk.Label(appearance, text="Workspace appearance", style="Panel.TLabel", font=("Segoe UI Semibold", round(14 * self.scale))).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(appearance, text="Changes preview immediately and persist for the next launch.", style="PanelMuted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 16))
        ttk.Label(appearance, text="Color theme", style="PanelMuted.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        themes = ttk.Combobox(appearance, textvariable=self.theme_var, values=tuple(THEMES), state="readonly", width=24)
        themes.grid(row=2, column=1, sticky="w", pady=6)
        themes.bind("<<ComboboxSelected>>", lambda _: self.preview_appearance())
        ttk.Label(appearance, text="Text size", style="PanelMuted.TLabel").grid(row=3, column=0, sticky="w", pady=6)
        scale_choices = ttk.Combobox(appearance, textvariable=self.text_scale_var, values=("85", "90", "100", "110", "120", "130", "140"), state="readonly", width=10)
        scale_choices.grid(row=3, column=1, sticky="w", pady=6)
        scale_choices.bind("<<ComboboxSelected>>", lambda _: self.preview_appearance())
        ttk.Label(appearance, text="%", style="PanelMuted.TLabel").grid(row=3, column=2, sticky="w")
        ttk.Label(appearance, text="Layout density", style="PanelMuted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
        density = ttk.Combobox(appearance, textvariable=self.density_var, values=("Compact", "Comfortable", "Cozy"), state="readonly", width=18)
        density.grid(row=4, column=1, sticky="w", pady=6)
        density.bind("<<ComboboxSelected>>", lambda _: self.preview_appearance())
        ttk.Checkbutton(appearance, text="Reduce decorative motion", variable=self.reduced_motion_var, command=self.preview_appearance).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 6))
        ttk.Button(appearance, text="Save appearance", style="Accent.TButton", command=self.save_appearance).grid(row=6, column=0, sticky="w", pady=(14, 0))
        ttk.Button(appearance, text="Replay quick tour", command=self.open_tutorial).grid(row=6, column=1, sticky="w", pady=(14, 0))
        appearance.columnconfigure(1, weight=0)
        appearance.columnconfigure(2, weight=1)

        paths = ttk.Frame(storage_tab, style="Panel.TFrame", padding=16)
        paths.pack(fill="x", pady=(0, 12))
        ttk.Label(paths, text="Library folders", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        roots_area = ttk.Frame(paths, style="Panel.TFrame")
        roots_area.pack(fill="x", pady=8)
        self.roots_list = Listbox(roots_area, height=6, bg=self.theme.panel_alt, fg=self.theme.text, selectbackground=self.theme.selection, selectforeground=self.theme.text, relief="flat", highlightthickness=1, highlightbackground=self.theme.border, font=("Segoe UI", max(9, round(10 * self.scale))))
        self.roots_list.pack(side=LEFT, fill="x", expand=True)
        self.custom_text_widgets.append(self.roots_list)
        root_actions = ttk.Frame(roots_area, style="Panel.TFrame")
        root_actions.pack(side=LEFT, fill="y", padx=(8, 0))
        ttk.Button(root_actions, text="Add…", command=self.add_root).pack(fill="x")
        ttk.Button(root_actions, text="Remove", command=self.remove_root).pack(fill="x", pady=6)
        for root_path in self.config.library.roots:
            self.roots_list.insert(END, str(root_path))

        storage = ttk.Frame(storage_tab, style="Panel.TFrame", padding=16)
        storage.pack(fill="x", pady=(0, 12))
        ttk.Label(storage, text="Index storage", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(storage, text="Database", style="PanelMuted.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(storage, textvariable=self.db_path_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(storage, text="Browse…", command=self.choose_database).grid(row=1, column=2, padx=(8, 0))
        ttk.Label(storage, text="Preview cache", style="PanelMuted.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(storage, textvariable=self.cache_path_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(storage, text="Browse…", command=self.choose_cache).grid(row=2, column=2, padx=(8, 0))
        storage.columnconfigure(1, weight=1)

        options = ttk.Frame(scan_tab, style="Panel.TFrame", padding=18)
        options.pack(fill="x")
        ttk.Label(options, text="Scan options", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(0, 7))
        ttk.Checkbutton(options, text="Create previews and thumbnails", variable=self.derive_var).pack(anchor="w")
        ttk.Checkbutton(options, text="Force content re-hash on the next scan", variable=self.rehash_var).pack(anchor="w", pady=4)
        row = ttk.Frame(scan_tab)
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
        self.header.set_status("Library ready")
        self.progress_text.set("Ready")
        self.refresh_all()
        if not self.ui_settings.tutorial_completed:
            self.root.after(350, self.open_tutorial)

    def _fatal_startup(self, exc: Exception, details: str) -> None:
        self.header.set_status("Startup failed")
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
            self.header.set_status(f"{name.capitalize()} running")
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
        self.header.set_status("Library ready")
        self.long_job = None

    def _job_running(self) -> bool:
        return self.long_job is not None and self.long_job.is_alive()

    def _work_running(self) -> bool:
        """Whether any engine operation still owns a live worker thread."""
        with self.worker_lock:
            return bool(self.workers)

    def _schedule_responsive_layout(self, event: Any) -> None:
        if event.widget is not self.root:
            return
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(80, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self.resize_job = None
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        compact = width < 1080
        self.compact_layout = compact
        self.sidebar.configure(width=76 if compact else 186)
        self.content.configure(padding=(8, 8, 10 if compact else 18, 8))
        for key, button in self.nav_buttons.items():
            label, short = self.nav_labels[key]
            button.configure(
                text=short if compact else f"  {label}",
                style=("Selected.CompactNav.TButton" if key == self.current_page else "CompactNav.TButton")
                if compact
                else ("Selected.Nav.TButton" if key == self.current_page else "Nav.TButton"),
            )
        self.engine_version_label.configure(text=__version__ if compact else f"Engine {__version__}")

        # V1 never permits a window narrower than 900 px. Four compact cards
        # still fit at that width and preserve far more vertical room for the
        # actual library than a visually tempting 2x2 arrangement would.
        columns = 4
        for index, card in enumerate(self.stats_cards):
            card.grid_forget()
            row, column = divmod(index, columns)
            card.grid(row=row, column=column, sticky="nsew", padx=(0, 8), pady=(0, 8))
        for column in range(4):
            self.cards_container.columnconfigure(column, weight=1 if column < columns else 0, uniform="stats")
        if height < 700:
            self.cards_container.pack_forget()
        elif not self.cards_container.winfo_manager():
            self.cards_container.pack(fill="x", pady=(0, 12), before=self.library_paned)

        if width < 1060:
            self.search_entry.grid(row=0, column=0, columnspan=4, sticky="ew")
            self.search_clear_button.grid(row=0, column=4, padx=(6, 0))
            self.type_selector.grid(row=1, column=0, sticky="w", pady=(7, 0), padx=0)
            self.search_button.grid(row=1, column=1, sticky="w", pady=(7, 0), padx=7)
            self.add_scan_button.grid(row=1, column=2, columnspan=3, sticky="e", pady=(7, 0), padx=0)
            self.results.configure(displaycolumns=("name", "type", "size"))
        else:
            self.search_entry.grid(row=0, column=0, columnspan=1, sticky="ew")
            self.search_clear_button.grid(row=0, column=1, padx=(6, 0), pady=0)
            self.type_selector.grid(row=0, column=2, padx=7, pady=0)
            self.search_button.grid(row=0, column=3, padx=0, pady=0)
            self.add_scan_button.grid(row=0, column=4, padx=(7, 0), pady=0)
            self.results.configure(displaycolumns=("name", "type", "size", "date", "dimensions"))

        available = max(360, self.results_panel.winfo_width() - 35)
        self.results.column("name", width=max(190, round(available * 0.40)))
        self.results.column("type", width=max(68, round(available * 0.10)))
        self.results.column("size", width=max(72, round(available * 0.10)))
        self.results.column("date", width=max(120, round(available * 0.22)))
        self.results.column("dimensions", width=max(90, round(available * 0.16)))

    # ── navigation and refresh ─────────────────────────────────────────────

    def show_page(self, name: str) -> None:
        self.current_page = name
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill=BOTH, expand=True)
        for key, button in self.nav_buttons.items():
            if self.compact_layout:
                style = "Selected.CompactNav.TButton" if key == name else "CompactNav.TButton"
            else:
                style = "Selected.Nav.TButton" if key == name else "Nav.TButton"
            button.configure(style=style)
        self.header.transition(self.nav_labels[name][0])
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
        self._animate_count(self.card_values["assets"], total)
        self._animate_count(self.card_values["images"], int(types.get("image", 0)))
        self._animate_count(self.card_values["videos"], int(types.get("video", 0)))
        self._animate_count(self.card_values["documents"], int(types.get("document", 0)))

    def _animate_count(self, variable: StringVar, target: int, step: int = 0) -> None:
        if self.ui_settings.reduced_motion or target == 0:
            variable.set(f"{target:,}")
            return
        frames = 14
        value = round(target * min(1.0, (step + 1) / frames))
        variable.set(f"{value:,}")
        if step + 1 < frames:
            self.root.after(18, self._animate_count, variable, target, step + 1)

    # ── search and asset detail ────────────────────────────────────────────

    def focus_search(self) -> None:
        self.show_page("library")
        self.search_entry.focus_set()

    def clear_search(self) -> None:
        self.search_entry.clear()
        self.type_filter.set("all")
        self.run_search(reset=True)

    def clear_active_search(self) -> None:
        if self.current_page == "library":
            self.clear_search()
        elif self.current_page == "activity":
            self.activity_search.clear()
            self.filter_activity()
        elif self.current_page == "plugins":
            self.plugin_search.clear()
            self.filter_plugins()

    def apply_quick_search(self, query: str) -> None:
        self.search_text.set(query)
        self.type_filter.set("all")
        self.run_search(reset=True)

    def open_tutorial(self) -> None:
        if any(isinstance(child, TutorialDialog) for child in self.root.winfo_children()):
            return
        TutorialDialog(self.root, self.theme, self.scale, self._tutorial_finished)

    def _tutorial_finished(self) -> None:
        self.ui_settings.tutorial_completed = True
        save_ui_settings(self.config, self.ui_settings)
        if not self.config.library.roots and messagebox.askyesno(
            "Add your first folder?",
            "The tour is complete. Would you like to choose a media folder now?",
        ):
            self.choose_scan_folder()

    def open_help(self) -> None:
        if any(isinstance(child, HelpCenterDialog) for child in self.root.winfo_children()):
            return
        HelpCenterDialog(self.root, self.theme, self.scale, self.open_tutorial)

    def preview_appearance(self) -> None:
        try:
            self.ui_settings.text_scale = int(self.text_scale_var.get())
        except ValueError:
            self.ui_settings.text_scale = 100
        self.ui_settings.theme = self.theme_var.get()
        self.ui_settings.density = self.density_var.get()
        self.ui_settings.reduced_motion = self.reduced_motion_var.get()
        self.ui_settings.normalize()
        self.theme = THEMES[self.ui_settings.theme]
        self.scale = self.ui_settings.text_scale / 100.0
        self.root.configure(bg=self.theme.bg)
        self._configure_style()
        self.header.apply_theme(
            self.theme,
            self.scale,
            self.ui_settings.reduced_motion,
        )
        self.detail.configure(
            bg=self.theme.panel,
            fg=self.theme.text,
            insertbackground=self.theme.text,
            font=("Cascadia Mono", max(8, round(9 * self.scale))),
        )
        self.roots_list.configure(
            bg=self.theme.panel_alt,
            fg=self.theme.text,
            selectbackground=self.theme.selection,
            selectforeground=self.theme.text,
            highlightbackground=self.theme.border,
            font=("Segoe UI", max(9, round(10 * self.scale))),
        )
        self._apply_responsive_layout()

    def save_appearance(self) -> None:
        self.preview_appearance()
        path = save_ui_settings(self.config, self.ui_settings)
        self.progress_text.set(f"Appearance saved · {path.name}")

    def run_search(self, *, reset: bool) -> None:
        if not self.engine.started:
            return
        if reset:
            self.search_offset = 0
        raw = self.search_entry.value()
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
                    preview_width = min(340, max(250, self.detail_panel.winfo_width() - 34))
                    preview_height = min(205, round(preview_width * 0.62))
                    image.thumbnail((preview_width, preview_height), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGB", (preview_width, preview_height), self.theme.panel)
                    fitted = ImageOps.contain(image.convert("RGB"), (preview_width, preview_height))
                    canvas.paste(fitted, ((preview_width - fitted.width) // 2, (preview_height - fitted.height) // 2))
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
        self.all_scans, self.all_errors = payload
        self.filter_activity()

    def filter_activity(self) -> None:
        raw = self.activity_search.value().lower() if hasattr(self, "activity_search") else ""
        scans = [item for item in getattr(self, "all_scans", []) if not raw or raw in " ".join(str(value) for value in item.values()).lower()]
        errors = [item for item in getattr(self, "all_errors", []) if not raw or raw in " ".join(str(value) for value in item.values()).lower()]
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
        dialog.configure(bg=self.theme.bg)
        text = Text(dialog, bg=self.theme.panel, fg=self.theme.text, insertbackground=self.theme.text, wrap="word", font=("Cascadia Mono", max(8, round(9 * self.scale))), padx=12, pady=12)
        text.pack(fill=BOTH, expand=True, padx=10, pady=10)
        text.insert("1.0", json.dumps(item, indent=2, ensure_ascii=False))
        text.configure(state="disabled")

    def refresh_plugins(self) -> None:
        from ..plugins import PluginManager

        self._run_background("analyzers", lambda: PluginManager(self.engine).catalog(), self._show_plugins)

    def _show_plugins(self, plugins: list[dict[str, object]]) -> None:
        self.all_plugins = plugins
        self.filter_plugins()

    def filter_plugins(self) -> None:
        raw = self.plugin_search.value().lower() if hasattr(self, "plugin_search") else ""
        self.plugins_tree.delete(*self.plugins_tree.get_children())
        for plugin in getattr(self, "all_plugins", []):
            if raw and raw not in " ".join(str(value) for value in plugin.values()).lower():
                continue
            task_counts = plugin.get("tasks") or {}
            state = ", ".join(f"{key}: {value}" for key, value in task_counts.items()) if isinstance(task_counts, dict) else ""
            self.plugins_tree.insert("", END, iid=str(plugin["plugin_id"]), values=(plugin.get("plugin_id"), plugin.get("version"), plugin.get("kind") or plugin.get("transport"), "Yes" if plugin.get("enabled") else "No", state or "—", plugin.get("load_error") or plugin.get("description") or ""))

    def _selected_plugin_row(self) -> dict[str, object] | None:
        selected = self.plugins_tree.selection()
        if not selected:
            return None
        plugin_id = selected[0]
        return next(
            (row for row in getattr(self, "all_plugins", []) if row.get("plugin_id") == plugin_id),
            None,
        )

    def toggle_selected_plugin(self) -> None:
        row = self._selected_plugin_row()
        if row is None:
            messagebox.showinfo("Select an analyzer", "Choose an analyzer in the shop first.")
            return
        if self._job_running():
            messagebox.showinfo("Analyzer is running", "Wait for the current analyzer or scan to finish first.")
            return
        plugin_id = str(row["plugin_id"])
        enable = not bool(row.get("enabled"))
        grant_network = False
        plugin = self.engine.plugins.get(plugin_id)
        if enable and plugin is not None and plugin.info.network and not self.config.plugins.allow_network:
            grant_network = messagebox.askyesno(
                "Allow local AI/API access?",
                f"{plugin_id} declares network access. Allow analyzer network access?\n\n"
                "This is required for LM Studio and HTTP analyzers; originals remain read-only.",
            )
            if not grant_network:
                return

        def work() -> dict[str, object]:
            from ..plugins import PluginManager

            return cast(
                dict[str, object],
                PluginManager(self.engine).set_enabled(
                    plugin_id, enable, grant_network=grant_network
                ),
            )

        def done(_: dict[str, object]) -> None:
            self.progress_text.set(f"{plugin_id} {'enabled' if enable else 'disabled'}")
            self.refresh_plugins()

        self._run_background("plugin setting", work, done)

    def add_plugin_api(self) -> None:
        if self._job_running():
            messagebox.showinfo("Job in progress", "Wait for the current scan or analyzer first.")
            return
        base_url = simpledialog.askstring(
            "Add analyzer API",
            "Analyzer base URL (it must expose /manifest, /health, and /analyze):",
            initialvalue="http://127.0.0.1:9000",
            parent=self.root,
        )
        if not base_url:
            return
        token = simpledialog.askstring(
            "Optional API token",
            "Bearer token (leave blank when the local service has no token):",
            show="*",
            parent=self.root,
        )
        from ..plugins import endpoint_is_local

        allow_external = False
        if not endpoint_is_local(base_url):
            allow_external = messagebox.askyesno(
                "External analyzer endpoint",
                "This URL is not loopback. Registering it can send media metadata or derivatives "
                "to another computer. Continue?",
            )
            if not allow_external:
                return

        def work() -> dict[str, object]:
            from ..plugins import PluginManager

            return cast(
                dict[str, object],
                PluginManager(self.engine).register_remote(
                    base_url,
                    auth_token=token or None,
                    allow_external=allow_external,
                ).as_dict(),
            )

        def done(result: dict[str, object]) -> None:
            messagebox.showinfo(
                "Analyzer added",
                f"Registered {result['plugin_id']} {result['version']}.\n"
                "It is ready to run from the analyzer shop.",
            )
            self.refresh_plugins()

        self._run_background("API registration", work, done)

    def configure_lm_studio(self) -> None:
        if self._job_running():
            messagebox.showinfo("Job in progress", "Wait for the current scan or analyzer first.")
            return
        current = self.config.plugin_config("local.lm-studio")
        base_url = simpledialog.askstring(
            "LM Studio server",
            "LM Studio URL (start its server from the Developer tab):",
            initialvalue=str(current.get("base_url") or "http://127.0.0.1:1234"),
            parent=self.root,
        )
        if not base_url:
            return
        model = simpledialog.askstring(
            "LM Studio model",
            "Model identifier. Leave blank to automatically use the first model LM Studio exposes:",
            initialvalue=str(current.get("model") or ""),
            parent=self.root,
        )
        token = simpledialog.askstring(
            "LM Studio API token",
            "Optional token. Leave blank to keep the existing token or use an unauthenticated local server:",
            show="*",
            parent=self.root,
        )
        send_image = messagebox.askyesno(
            "Use a vision model?",
            "Send a 512-pixel preview to LM Studio for images?\n\n"
            "Choose No for text-only models. Originals are never sent or modified.",
        )
        values = dict(current)
        values.update(
            {
                "base_url": base_url,
                "model": (model or "").strip(),
                "send_image": send_image,
                "structured_output": True,
            }
        )
        if token:
            values["api_token"] = token

        def work() -> None:
            from ..plugins import PluginManager

            manager = PluginManager(self.engine)
            manager.configure("local.lm-studio", values)
            manager.set_enabled("local.lm-studio", True, grant_network=True)

        def done(_: object) -> None:
            messagebox.showinfo(
                "LM Studio connected",
                "LM Studio enrichment is enabled. Select it and choose Run selected after indexing files.",
            )
            self.refresh_plugins()

        self._run_background("LM Studio setup", work, done)

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
            self.header.set_status("Closing safely…")
            return
        if self._work_running():
            self.close_pending = True
            self.header.set_status("Finishing current read…")
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
