"""Qt/PySide6 File Indexer Studio window.

This module is a frontend only. It talks directly to :class:`MediaEngine` and
never shells through the CLI or HTTP API.

Theme changes rebuild the workspace rather than mutating every widget in
place. Studio's components are owner-drawn, so a palette touches gradients,
glows, shadow polarity, and corner radii at once; rebuilding is both shorter
and less likely to leave one stale widget painting last month's colors.
"""

from __future__ import annotations

import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import (
    QEasingCurve,
    QFileSystemWatcher,
    QPoint,
    QPropertyAnimation,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..assistant import Assistant, AssistantEvent
from ..config import Config
from ..core.control import CancelToken, ProgressEvent
from ..engine import MediaEngine
from ..errors import ConfigError
from ..gui.config_store import save_desktop_config
from ..maintenance import (
    RelocationMode,
    describe_storage,
    is_temporary_location,
    master_reset,
    plan_relocation,
    relocate_storage,
)
from ..search import parse_query
from .assistant_dialog import AssistantDialog
from .components import (
    AnimatedButton,
    AssetCard,
    EmptyState,
    GradientCanvas,
    InspectorPanel,
    StatusDock,
    Toast,
    human_size,
    open_item,
)
from .player import VideoPlayerDialog, group_moments
from .dialogs import (
    AnalyzerStoreDialog,
    EraseConfirmDialog,
    FolderManagerDialog,
    SettingsDialog,
    clear_layout,
)
from .tokens import (
    AUTO_PALETTE,
    PALETTES,
    TOKENS,
    StudioTokens,
    application_stylesheet,
    palette,
    palette_choices,
)
from .workers import EngineWorker

LM_STUDIO_ID = "local.lm-studio"

#: Tile widths per named size. "Cinema" exists for video-heavy libraries where
#: a legible frame matters more than how many fit on screen.
CARD_WIDTHS = {"Compact": 216, "Comfortable": 245, "Spacious": 286, "Cinema": 340}

#: Namespaces whose labels are *not* worth showing on a tile. An allowlist
#: was the wrong shape here: the core is built so anyone can ship an analyzer
#: emitting a namespace nobody has heard of, and a tile that only renders three
#: known namespaces silently hides every one of them — which is exactly what
#: happened to the local tagger's `content.tag`. So: show what analyzers found,
#: and name the few kinds of output a person does not want on a thumbnail.
HIDDEN_TILE_NAMESPACES = ("visual.", "embedding.", "llm.summary", "exif.", "file.")


def _as_float(value: object, default: float) -> float:
    """Coerce a stored preference to a float.

    QSettings round-trips values through the registry on Windows, so a float
    written last session comes back as ``"0.65"``. Reading it back defensively
    is cheaper than discovering the difference at paint time.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_str_list(value: object) -> list[str]:
    """Coerce a loosely-typed payload field into a list of strings."""
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []


def _thumbnail_for(config: Config, derivatives: list[dict[str, Any]]) -> str | None:
    thumbnails = [item for item in derivatives if item.get("kind") == "thumb"]
    if not thumbnails:
        return None
    chosen = min(thumbnails, key=lambda item: abs(int(item.get("variant") or 0) - 512))
    root = config.storage.derivatives_path.resolve()
    candidate = (root / str(chosen.get("rel_path") or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return str(candidate) if candidate.is_file() else None


def _decorate(items: list[dict[str, Any]], labels: dict[int, list[dict[str, Any]]]) -> None:
    """Attach analyzer output to search hits for the tile and inspector.

    Highest confidence first, because a tile has room for six labels and the
    six the model was surest about are the six worth showing.
    """
    for item in items:
        rows = labels.get(int(item["id"]), [])
        tags: list[str] = []
        summary = ""
        scored: list[tuple[float, str]] = []
        for row in rows:
            namespace = str(row.get("namespace") or "")
            if namespace == "llm.summary":
                value = row.get("value")
                if isinstance(value, dict) and value.get("text"):
                    summary = str(value["text"])
                continue
            if namespace.startswith(HIDDEN_TILE_NAMESPACES):
                continue
            label = str(row.get("label") or "")
            if not label:
                continue
            try:
                confidence = float(row.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            scored.append((confidence, label))
        for _confidence, label in sorted(scored, key=lambda pair: -pair[0]):
            if label not in tags:
                tags.append(label)
        item["tags"] = tags[:6]
        item["all_tags"] = tags
        item["ai_summary"] = summary


class StudioWindow(QMainWindow):
    """The polished, thumbnail-first Studio frontend."""

    library_changed = Signal()

    def __init__(
        self,
        config: Config,
        *,
        engine: MediaEngine | None = None,
        tokens: StudioTokens = TOKENS,
    ) -> None:
        super().__init__()
        self.config = config
        self.engine = engine or MediaEngine(config)
        self.settings = QSettings("File Indexer", "Studio")
        self.base_tokens = tokens
        self.tokens = self._resolve_tokens(tokens)
        self.video_hover_enabled = bool(self.settings.value("video_hover", True, type=bool))
        self.auto_select_results = bool(self.settings.value("auto_select", True, type=bool))
        self.show_tags = bool(self.settings.value("show_tags", True, type=bool))
        self.card_size_name = str(self.settings.value("card_size", "Comfortable"))
        self.card_width = CARD_WIDTHS.get(self.card_size_name, 245)
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(max(4, min(12, self.thread_pool.maxThreadCount())))
        self._workers: list[EngineWorker] = []
        self.search_generation = 0
        self.current_query = ""
        self.current_filter = ""
        self.current_view = "All media"
        self.active_facets: list[str] = []
        self.facet_groups: list[dict[str, Any]] = []
        self.cards: list[AssetCard] = []
        self.selected_card: AssetCard | None = None
        self.scan_token: CancelToken | None = None
        self.analysis_token: CancelToken | None = None
        self.scan_running = False
        self.analysis_running = False
        self.installing_model = ""
        self._reflow_pending = False
        self._last_columns = 0
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)
        self.folder_dialog: FolderManagerDialog | None = None
        self.analyzer_dialog: AnalyzerStoreDialog | None = None
        self.settings_dialog: SettingsDialog | None = None
        self.assistant_dialog: AssistantDialog | None = None
        self.assistant: Assistant | None = None
        #: Open players. Held because a QDialog with no Python reference is
        #: collected mid-playback, which closes the window under the user.
        self.player_dialogs: list[VideoPlayerDialog] = []

        self.setWindowTitle("File Indexer Studio")
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        icon_path = resource_root / "assets" / "file-indexer-studio.svg"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(980, 680)
        self.resize(1440, 900)
        self.setAcceptDrops(True)
        self.setObjectName("StudioWindow")
        self._build_ui()
        self._wire_shortcuts()
        self._follow_system_theme()
        self._configure_automation()
        self._start_engine()

    # ── theming ───────────────────────────────────────────────────────────

    @property
    def palette_choice(self) -> str:
        """The stored theme name, which may be the follow-the-system option."""
        stored = str(self.settings.value("palette", "") or "")
        return stored if stored in PALETTES or stored == AUTO_PALETTE else TOKENS.name

    def open_theme_menu(self) -> None:
        """Pick a palette from the header, applying it immediately."""
        menu = QMenu(self)
        current = self.palette_choice
        for name in palette_choices():
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == current)
        anchor = self.theme_button.mapToGlobal(QPoint(0, self.theme_button.height() + 6))
        # exec() rather than a triggered signal: applying a palette rebuilds
        # the workspace, which deletes the button the menu is anchored to.
        chosen = menu.exec(anchor)
        if chosen is not None and chosen.text() != current:
            self.choose_palette(chosen.text())

    def choose_palette(self, name: str) -> None:
        """Adopt and remember one palette choice."""
        self.settings.setValue("palette", name)
        self.apply_tokens(palette(name))
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.palette_combo.setCurrentText(name)
        self.show_toast(
            "Following your Windows light and dark setting."
            if name == AUTO_PALETTE
            else f"{name} theme applied."
        )

    def _system_theme_changed(self, _scheme: object = None) -> None:
        """Repaint when Windows flips to dark, but only if asked to follow it."""
        if self.palette_choice == AUTO_PALETTE:
            self.apply_tokens(palette(AUTO_PALETTE))

    def _resolve_tokens(self, base: StudioTokens) -> StudioTokens:
        """Apply the user's accent, gradient, density, and motion preferences."""
        tokens = base
        accent = str(self.settings.value("accent", "") or "")
        if accent:
            tokens = tokens.with_accent(accent)
        tokens = tokens.with_intensity(_as_float(self.settings.value("intensity", 1.0), 1.0))
        tokens = tokens.with_density(_as_float(self.settings.value("density", 1.0), 1.0))
        if not bool(self.settings.value("animations", True, type=bool)):
            tokens = tokens.without_motion()
        return tokens

    def apply_tokens(self, base: StudioTokens) -> None:
        """Adopt a palette immediately, preserving the current view."""
        self.base_tokens = base
        self.tokens = self._resolve_tokens(base)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(application_stylesheet(self.tokens))
        search_text = self.search.text() if hasattr(self, "search") else ""
        self._build_ui()
        self.search.setText(search_text)
        self._restore_selection_state()
        for dialog in (self.folder_dialog, self.analyzer_dialog):
            if dialog is not None:
                dialog.tokens = self.tokens
        self._last_columns = 0
        self.refresh_library()
        self.refresh_facets()

    def _restore_selection_state(self) -> None:
        for name, button in self.nav_buttons.items():
            button.setChecked(name == self.current_view)
        self.context_label.setText(
            "Your library" if self.current_view == "All media" and not self.current_query else self.current_view
        )

    # ── construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = GradientCanvas(self.tokens, self)
        self.setCentralWidget(root)
        self.canvas = root
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_sidebar())

        workspace = QWidget(root)
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(28, 22, 24, 18)
        workspace_layout.setSpacing(14)
        workspace_layout.addLayout(self._build_header())
        workspace_layout.addLayout(self._build_search())
        workspace_layout.addLayout(self._build_filters())

        content = QHBoxLayout()
        content.setSpacing(14)
        self.grid_area = QScrollArea(workspace)
        self.grid_area.setWidgetResizable(True)
        self.grid_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_area.viewport().setAcceptDrops(True)
        self.grid_host = QWidget(self.grid_area)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(2, 2, 6, 12)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.grid_area.setWidget(self.grid_host)

        self.empty_state = EmptyState(tokens=self.tokens, parent=self.grid_host)
        self.empty_state.add_folder_requested.connect(self.choose_folder)
        self.grid.addWidget(self.empty_state, 0, 0)
        content.addWidget(self.grid_area, 1)

        self.inspector = InspectorPanel(tokens=self.tokens, parent=workspace)
        self.inspector.open_requested.connect(self.open_asset)
        self.inspector.reveal_requested.connect(self.reveal_asset)
        self.inspector.favorite_requested.connect(self.favorite_asset)
        content.addWidget(self.inspector, 0)
        workspace_layout.addLayout(content, 1)

        self.status = StatusDock(tokens=self.tokens, parent=workspace)
        self.status.cancel_requested.connect(self.cancel_running_job)
        workspace_layout.addWidget(self.status)
        shell.addWidget(workspace, 1)

        self.cards = []
        self.selected_card = None
        self.toast = Toast(self.tokens, root)
        self.toast.raise_()

    def _build_sidebar(self) -> QWidget:
        sidebar = _Sidebar(self.tokens, self)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(7)

        brand_row = QHBoxLayout()
        mark = _BrandMark(self.tokens, sidebar)
        brand_row.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel("File Indexer", sidebar)
        name.setStyleSheet("font-size:14px; font-weight:700;")
        edition = QLabel("STUDIO", sidebar)
        edition.setStyleSheet(
            f"color:{self.tokens.brand}; font-size:9px; font-weight:800; letter-spacing:1px;"
        )
        brand.addWidget(name)
        brand.addWidget(edition)
        brand_row.addLayout(brand, 1)
        layout.addLayout(brand_row)
        layout.addSpacing(22)

        layout.addWidget(self._eyebrow("LIBRARY", sidebar))
        self.nav_buttons: dict[str, AnimatedButton] = {}
        for label, icon, query in (
            ("All media", "◫", ""),
            ("Recent", "◷", "sort:-captured"),
            ("Photos", "▧", "type:image"),
            ("Videos", "▶", "type:video"),
            ("Documents", "▤", "type:document"),
            ("AI tagged", "✦", "has:llm.category"),
            ("Favorites", "♡", "user.label:favorite"),
        ):
            button = AnimatedButton(
                label, variant="ghost", icon_text=icon, tokens=self.tokens, parent=sidebar
            )
            button.setCheckable(True)
            button.clicked.connect(partial(self.apply_smart_view, label, query))
            layout.addWidget(button)
            self.nav_buttons[label] = button
        self.nav_buttons[self.current_view].setChecked(True)

        layout.addSpacing(16)
        layout.addWidget(self._eyebrow("TOOLS", sidebar))
        for label, icon, callback in (
            ("Assistant", "◆", self.open_assistant),
            ("Library folders", "▰", self.open_folder_manager),
            ("AI Model Store", "✦", self.open_analyzer_store),
            ("Settings", "⚙", self.open_settings),
        ):
            button = AnimatedButton(
                label, variant="ghost", icon_text=icon, tokens=self.tokens, parent=sidebar
            )
            button.clicked.connect(callback)
            layout.addWidget(button)
        layout.addStretch(1)

        automation = _SoftPanel(self.tokens, sidebar)
        auto_layout = QVBoxLayout(automation)
        auto_layout.setContentsMargins(12, 12, 12, 12)
        auto_layout.setSpacing(7)
        auto_title = QLabel("Automatic indexing", automation)
        auto_title.setStyleSheet("font-weight:650;")
        auto_layout.addWidget(auto_title)
        auto_copy = QLabel("Watch folders and quietly refresh when files change.", automation)
        auto_copy.setWordWrap(True)
        auto_copy.setStyleSheet(f"color:{self.tokens.ink_soft}; font-size:11px;")
        auto_layout.addWidget(auto_copy)
        self.auto_button = AnimatedButton(
            "Auto sync on", variant="chip", icon_text="●", tokens=self.tokens, parent=automation
        )
        self.auto_button.setCheckable(True)
        self.auto_button.setChecked(bool(self.settings.value("auto_sync", True, type=bool)))
        self.auto_button.setText("Auto sync on" if self.auto_button.isChecked() else "Auto sync off")
        self.auto_button.clicked.connect(self.toggle_auto_sync)
        auto_layout.addWidget(self.auto_button)
        layout.addWidget(automation)
        version = QLabel(f"Engine {__version__}", sidebar)
        version.setStyleSheet(f"color:{self.tokens.ink_faint}; font-size:9px;")
        layout.addWidget(version, 0, Qt.AlignmentFlag.AlignHCenter)
        return sidebar

    def _eyebrow(self, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setStyleSheet(
            f"color:{self.tokens.ink_faint}; font-size:9px; font-weight:800; letter-spacing:1px;"
        )
        return label

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.context_label = QLabel("Your library", self)
        font = QFont(self.font())
        font.setPointSize(24)
        font.setWeight(QFont.Weight.DemiBold)
        self.context_label.setFont(font)
        titles.addWidget(self.context_label)
        self.subtitle_label = QLabel(
            "Everything you care about, visually organized and instantly searchable.", self
        )
        self.subtitle_label.setStyleSheet(f"color:{self.tokens.ink_soft}; font-size:12px;")
        titles.addWidget(self.subtitle_label)
        row.addLayout(titles, 1)
        self.count_label = QLabel("Starting…", self)
        self.count_label.setStyleSheet(
            f"background:{self.tokens.surface}; color:{self.tokens.ink_soft};"
            f"border:1px solid {self.tokens.border};"
            f"border-radius:{self.tokens.radius_sm}px; padding:9px 12px;"
        )
        row.addWidget(self.count_label)
        # A theme is the one setting people change on a whim, so it gets a
        # one-click home in the header rather than living three tabs deep.
        self.theme_button = AnimatedButton(
            self.palette_choice, variant="secondary", icon_text="◐", tokens=self.tokens, parent=self
        )
        self.theme_button.setToolTip("Switch theme (Ctrl+T)")
        self.theme_button.clicked.connect(self.open_theme_menu)
        row.addWidget(self.theme_button)
        self.add_button = AnimatedButton(
            "Add folder", variant="primary", icon_text="+", tokens=self.tokens, parent=self
        )
        self.add_button.clicked.connect(self.choose_folder)
        row.addWidget(self.add_button)
        return row

    def _build_search(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(
            "Search people, places, filenames, camera details, or AI labels…"
        )
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(48)
        self.search.textChanged.connect(self._schedule_search)
        row.addWidget(self.search, 1)
        self.rescan_button = AnimatedButton(
            "Refresh library", variant="secondary", icon_text="↻", tokens=self.tokens, parent=self
        )
        self.rescan_button.clicked.connect(self.scan_configured)
        row.addWidget(self.rescan_button)
        self.reset_button = AnimatedButton("Reset", variant="ghost", tokens=self.tokens, parent=self)
        self.reset_button.setToolTip("Clear the search and every active view filter")
        self.reset_button.clicked.connect(self.reset_filters)
        row.addWidget(self.reset_button)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(230)
        self.search_timer.timeout.connect(self.refresh_library)
        return row

    def _build_filters(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(7)
        row = QHBoxLayout()
        row.setSpacing(7)
        label = QLabel("Show", self)
        label.setStyleSheet(f"color:{self.tokens.ink_soft};")
        row.addWidget(label)
        self.filter_buttons: dict[str, AnimatedButton] = {}
        for label_text, query in (
            ("Everything", ""),
            ("Photos", "type:image"),
            ("Videos", "type:video"),
            ("Documents", "type:document"),
            ("With a location", "has:gps"),
        ):
            button = AnimatedButton(label_text, variant="chip", tokens=self.tokens, parent=self)
            button.setCheckable(True)
            button.clicked.connect(partial(self.apply_filter, label_text, query))
            row.addWidget(button)
            self.filter_buttons[label_text] = button
        self.filter_buttons["Everything"].setChecked(True)
        row.addStretch(1)
        tip = QLabel("Hover videos to preview  ·  Drag folders here to add them", self)
        tip.setStyleSheet(f"color:{self.tokens.ink_faint}; font-size:10px;")
        row.addWidget(tip)
        column.addLayout(row)

        # A second row that only exists once a filter pack has produced values.
        # Building it from live facet counts rather than a hardcoded list is
        # what lets a pack installed this morning become a filter by lunch.
        self.facet_row = QHBoxLayout()
        self.facet_row.setSpacing(7)
        self.facet_buttons: dict[str, AnimatedButton] = {}
        column.addLayout(self.facet_row)
        return column

    def refresh_facets(self) -> None:
        """Rebuild the pack filter bar from what the library actually carries."""
        if not self.engine.started:
            return

        def load() -> list[dict[str, Any]]:
            from ..plugins import PluginManager

            return PluginManager(self.engine).facet_groups()

        worker = EngineWorker(load)
        worker.signals.result.connect(self._show_facets)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _show_facets(self, payload: object) -> None:
        if not isinstance(payload, list) or not hasattr(self, "facet_row"):
            return
        clear_layout(self.facet_row)
        self.facet_buttons.clear()
        self.facet_groups = cast(list[dict[str, Any]], payload)
        if not self.facet_groups:
            return
        for group in self.facet_groups:
            title = QLabel(str(group.get("title") or ""), self)
            title.setStyleSheet(
                f"color:{self.tokens.ink_faint}; font-size:9px; font-weight:800; letter-spacing:1px;"
            )
            self.facet_row.addWidget(title)
            for value in list(group.get("values") or [])[:6]:
                text = f"{value['display']}  {int(value['count']):,}"
                button = AnimatedButton(text, variant="chip", tokens=self.tokens, parent=self)
                button.setCheckable(True)
                query = str(value["query"])
                button.setChecked(query in self.active_facets)
                button.clicked.connect(partial(self.toggle_facet, query))
                self.facet_row.addWidget(button)
                self.facet_buttons[query] = button
        self.facet_row.addStretch(1)

    def toggle_facet(self, query: str) -> None:
        """Add or drop one pack facet value from the active query."""
        if query in self.active_facets:
            self.active_facets.remove(query)
        else:
            self.active_facets.append(query)
        button = self.facet_buttons.get(query)
        if button is not None:
            button.setChecked(query in self.active_facets)
            button.update()
        self.refresh_library()

    def _wire_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+K"), self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Ctrl+R"), self, self.scan_configured)
        QShortcut(QKeySequence("Ctrl+M"), self, self.open_analyzer_store)
        QShortcut(QKeySequence("Ctrl+J"), self, self.open_assistant)
        QShortcut(QKeySequence("Ctrl+T"), self, self.open_theme_menu)
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("Escape"), self, lambda: self.search.clear())

    def _follow_system_theme(self) -> None:
        """Listen once for the desktop switching between light and dark.

        Connected in the constructor rather than in ``_build_ui``, which runs
        again on every palette change and would otherwise stack a new
        connection each time.
        """
        hints = QGuiApplication.styleHints()
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is not None:  # pragma: no branch - present from Qt 6.5
            signal.connect(self._system_theme_changed)

    # ── engine and searching ──────────────────────────────────────────────

    def _start_engine(self) -> None:
        self.status.set_busy("Opening your library…")
        worker = EngineWorker(self.engine.start)
        worker.signals.result.connect(lambda _: self._engine_ready())
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _start_worker(self, worker: EngineWorker) -> None:
        """Retain a Python owner until Qt has delivered every worker signal."""
        self._workers.append(worker)

        def release() -> None:
            if worker in self._workers:
                self._workers.remove(worker)

        worker.signals.finished.connect(release)
        self.thread_pool.start(worker)

    def _engine_ready(self) -> None:
        self.status.set_ready()
        self.refresh_library()
        self.refresh_facets()
        self._warn_about_temporary_library()
        if self.auto_button.isChecked() and self.config.library.roots:
            QTimer.singleShot(1800, self.scan_configured)

    def _warn_about_temporary_library(self) -> None:
        """Say so loudly when the index sits somewhere Windows may erase.

        A library under ``%TEMP%`` looks healthy right up until a cleanup wipes
        it and every file has to be re-hashed, which reads to the user as the
        app looping forever rather than as data loss.
        """
        if not is_temporary_location(self.config.storage.db_path):
            return
        self.show_toast("Your index is in a temporary folder — open Settings ▸ Library to move it.")

    def _schedule_search(self) -> None:
        self.search_timer.start()

    def refresh_library(self) -> None:
        if not self.engine.started:
            return
        self.search_generation += 1
        generation = self.search_generation
        parts = [
            self.search.text().strip(),
            self.current_query,
            self.current_filter,
            *self.active_facets,
        ]
        raw = " ".join(part for part in parts if part).strip()
        self.count_label.setText("Finding the good stuff…")
        want_tags = self.show_tags

        def load() -> tuple[int, dict[str, Any]]:
            query = parse_query(raw, limit=120, vocabulary=self.engine.vocabulary())
            result = self.engine.search(query, with_facets=False)
            hits = list(result.hits)
            for item in hits:
                derivatives = self.engine.repos.derivatives.for_asset(int(item["id"]), kind="thumb")
                item["thumbnail_path"] = _thumbnail_for(self.config, derivatives)
            if want_tags and hits:
                # No namespace filter: whatever an analyzer produced is a tag
                # worth seeing, and _decorate drops the handful that are not.
                labels = self.engine.repos.annotations.live_labels(
                    [int(item["id"]) for item in hits], per_asset=24
                )
                _decorate(hits, labels)
            payload = dict(result)
            payload["items"] = hits
            return generation, payload

        worker = EngineWorker(load)
        worker.signals.result.connect(self._show_results)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _show_results(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2 or not isinstance(payload[1], dict):
            return
        generation = int(payload[0])
        result = cast(dict[str, Any], payload[1])
        if generation != self.search_generation:
            return
        items = list(result.get("items", []))
        total = int(result.get("total", 0))
        self.count_label.setText(f"{total:,} item{'s' if total != 1 else ''}")
        self._clear_cards()
        if not items:
            self.empty_state.set_message(
                "Nothing matches these filters. Clear the search, or pick a different view."
                if self.config.library.roots
                else "Drop a folder anywhere here. Studio will index it, build previews, and keep "
                "it searchable automatically."
            )
            self.empty_state.show()
            self.grid.addWidget(self.empty_state, 0, 0)
            return
        self.empty_state.hide()
        for item in items:
            card = AssetCard(
                item,
                tokens=self.tokens,
                parent=self.grid_host,
                video_hover_enabled=self.video_hover_enabled,
                preferred_width=self.card_width,
            )
            card.selected.connect(partial(self.select_asset, card))
            card.open_requested.connect(self.open_asset)
            self.cards.append(card)
            self._fade_in_card(card, len(self.cards) - 1)
        self._reflow_cards(force=True)
        if self.auto_select_results:
            self.select_asset(self.cards[0], self.cards[0].item)

    def _fade_in_card(self, card: AssetCard, index: int) -> None:
        """Stagger new result tiles so a completed search has visual continuity."""
        if self.tokens.motion_slow <= 0:
            return
        effect = QGraphicsOpacityEffect(card)
        effect.setOpacity(0.0)
        card.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", card)
        animation.setDuration(self.tokens.motion_slow)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        card.entry_animation = animation
        # Detaching the effect entirely would reset the widget's paint path
        # mid-frame; disabling it keeps the tile crisp and costs nothing.
        animation.finished.connect(lambda: effect.setEnabled(False))
        QTimer.singleShot(min(index, 12) * 24, animation.start)

    def _clear_cards(self) -> None:
        self.selected_card = None
        for card in self.cards:
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()
        self._last_columns = 0

    def _reflow_cards(self, *, force: bool = False) -> None:
        if not self.cards:
            return
        available = max(240, self.grid_area.viewport().width() - 10)
        columns = max(1, available // self.card_width)
        if not force and columns == self._last_columns:
            return
        self._last_columns = columns
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)

    # ── selection and actions ─────────────────────────────────────────────

    def select_asset(self, card: AssetCard, item: dict[str, Any]) -> None:
        if self.selected_card is not None and self.selected_card is not card:
            self.selected_card.set_selected(False)
        self.selected_card = card
        card.set_selected(True)
        self.inspector.show_item(item)

    def open_asset(self, item: dict[str, Any]) -> None:
        """Open a video in Studio's own player; hand anything else to the OS.

        The player is worth preferring for video specifically because it is the
        only place the timestamps mean anything: an external player knows the
        file, not what the analyzers found at 4:12.
        """
        if str(item.get("media_type") or "") == "video":
            self.play_video(item)
            return
        if not open_item(item):
            self.show_toast("That file is no longer available.")

    def play_video(self, item: dict[str, Any]) -> None:
        """Open the in-app player with this video's tagged moments loaded."""
        path = Path(str(item.get("path") or ""))
        if not path.exists():
            self.show_toast("That file is no longer available.")
            return
        asset_id = int(item.get("id") or 0)
        rows = (
            self.engine.repos.annotations.for_asset(asset_id) if asset_id else []
        )
        dialog = VideoPlayerDialog(item, group_moments(rows), self.tokens, self)
        dialog.open_externally_requested.connect(self._open_outside)
        dialog.finished.connect(lambda _result: self._player_closed(dialog))
        self.player_dialogs.append(dialog)
        dialog.show()

    def _open_outside(self, item: dict[str, Any]) -> None:
        if not open_item(item):
            self.show_toast("That file is no longer available.")

    def _player_closed(self, dialog: VideoPlayerDialog) -> None:
        if dialog in self.player_dialogs:
            self.player_dialogs.remove(dialog)
        dialog.deleteLater()

    def reveal_asset(self, item: dict[str, Any]) -> None:
        path = Path(str(item.get("path") or ""))
        if not path.exists():
            self.show_toast("That file is no longer available.")
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError as exc:
            self.show_toast(f"Could not open the folder: {exc}")

    def favorite_asset(self, item: dict[str, Any]) -> None:
        asset_id = int(item["id"])

        def work() -> None:
            self.engine.repos.annotations.add_user_annotation(asset_id, "user.label", "favorite")
            self.engine.pipeline().reindex_asset(asset_id)

        worker = EngineWorker(work)
        worker.signals.result.connect(lambda _: self.show_toast("Added to Favorites"))
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    # ── navigation and filters ────────────────────────────────────────────

    def apply_smart_view(self, label: str, query: str) -> None:
        self.current_view = label
        self.current_query = query
        self.current_filter = ""
        for name, button in self.nav_buttons.items():
            button.setChecked(name == label)
            button.update()
        for name, button in self.filter_buttons.items():
            button.setChecked(name == "Everything")
            button.update()
        self.context_label.setText(label)
        self.refresh_library()

    def apply_filter(self, label: str, query: str) -> None:
        self.current_filter = query
        for name, button in self.filter_buttons.items():
            button.setChecked(name == label)
            button.update()
        self.refresh_library()

    def reset_filters(self) -> None:
        """Return search and navigation to the complete library view."""
        self.search.clear()
        self.current_view = "All media"
        self.current_query = ""
        self.current_filter = ""
        self.active_facets.clear()
        for query, button in self.facet_buttons.items():
            del query
            button.setChecked(False)
            button.update()
        for name, button in self.nav_buttons.items():
            button.setChecked(name == "All media")
            button.update()
        for name, button in self.filter_buttons.items():
            button.setChecked(name == "Everything")
            button.update()
        self.context_label.setText("Your library")
        self.refresh_library()

    # ── scanning and automation ───────────────────────────────────────────

    def choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose a folder to index")
        if selected:
            self.add_folders([Path(selected)])

    def add_folders(self, folders: list[Path]) -> None:
        new_roots: list[Path] = []
        for folder in folders:
            resolved = folder.resolve()
            if resolved.is_dir() and resolved not in self.config.library.roots:
                self.config.library.roots.append(resolved)
                new_roots.append(resolved)
        if not new_roots:
            self.show_toast("That folder is already in your library.")
            return
        save_desktop_config(self.config)
        self._refresh_watched_paths()
        if self.folder_dialog is not None:
            self.folder_dialog.set_roots(list(self.config.library.roots))
        self.show_toast(
            f"Added {len(new_roots)} folder{'s' if len(new_roots) != 1 else ''}. Indexing now…"
        )
        self._start_scan(new_roots)

    def open_folder_manager(self) -> None:
        if self.folder_dialog is not None and self.folder_dialog.isVisible():
            self.folder_dialog.raise_()
            self.folder_dialog.activateWindow()
            return
        dialog = FolderManagerDialog(list(self.config.library.roots), self.tokens, self)
        dialog.add_requested.connect(self.choose_folder)
        dialog.scan_requested.connect(lambda root: self._start_scan([Path(root)]))
        dialog.remove_requested.connect(lambda root: self.remove_library_root(Path(root)))
        dialog.finished.connect(lambda _: setattr(self, "folder_dialog", None))
        self.folder_dialog = dialog
        dialog.open()

    def remove_library_root(self, root: Path) -> None:
        """Stop tracking a folder and safely forget only its index/cache data."""
        resolved = root.resolve()
        if resolved not in self.config.library.roots:
            self.show_toast("That folder is no longer in your library.")
            return
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job before removing a folder.")
            return
        answer = QMessageBox.question(
            self,
            "Remove folder from File Indexer?",
            f"Studio will stop watching:\n\n{resolved}\n\n"
            "Its index entries and generated previews will be removed. Your original files "
            "will never be deleted.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.config.library.roots = [
            item for item in self.config.library.roots if item.resolve() != resolved
        ]
        save_desktop_config(self.config)
        self._refresh_watched_paths()
        if self.folder_dialog is not None:
            self.folder_dialog.set_roots(list(self.config.library.roots))
        self.status.set_busy("Removing folder from the index...")
        worker = EngineWorker(lambda: self.engine.remove_indexed_root(resolved))
        worker.signals.result.connect(self._folder_removed)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _folder_removed(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        files = int(result.get("removed_files", 0))
        assets = int(result.get("removed_assets", 0))
        self.status.set_ready("Folder removed safely")
        self.show_toast(
            f"Folder removed — {files:,} file entries and {assets:,} unused items cleaned up."
        )
        self.refresh_library()

    def scan_configured(self) -> None:
        if not self.config.library.roots:
            self.choose_folder()
            return
        self._start_scan(list(self.config.library.roots))

    def _start_scan(self, roots: list[Path]) -> None:
        if self.scan_running:
            self.show_toast("Indexing is already running in the background.")
            return
        self.scan_running = True
        self.scan_token = CancelToken()
        token = self.scan_token
        self.status.set_busy("Looking for new and changed files…")

        def progress(event: ProgressEvent) -> None:
            worker.signals.progress.emit(event)

        def work() -> Any:
            return self.engine.scan(
                roots, resume=True, generate_derivatives=True, progress=progress, cancel=token
            )

        worker = EngineWorker(work)
        worker.signals.progress.connect(self._job_progress)
        worker.signals.result.connect(self._scan_finished)
        worker.signals.error.connect(self._job_failed)
        worker.signals.finished.connect(self._scan_stopped)
        self._start_worker(worker)

    def _job_progress(self, payload: object) -> None:
        event = payload if isinstance(payload, ProgressEvent) else None
        if event is None:
            return
        stage = event.stage.replace("_", " ").title()
        message = event.message or f"{stage} your library"
        self.status.set_busy(message, event.fraction)

    def _scan_finished(self, result: object) -> None:
        count = len(result) if isinstance(result, list) else 0
        self.status.set_ready("Library is up to date")
        self.show_toast(
            f"Indexing complete across {count or 1} folder{'s' if count != 1 else ''}."
        )
        self.refresh_library()

    def _job_failed(self, detail: str) -> None:
        if "OperationCancelled" in detail:
            self.status.set_ready("Paused safely")
            self.show_toast("Paused. It will resume next time.")
        else:
            self.status.set_ready("Something needs attention")
            self._show_worker_error(detail)

    def _scan_stopped(self) -> None:
        self.scan_running = False
        self.scan_token = None

    def cancel_running_job(self) -> None:
        if self.installing_model:
            self.cancel_model_install()
            return
        if self.scan_token is not None:
            self.scan_token.cancel("paused in Studio")
            self.status.set_busy("Pausing safely…")
        elif self.analysis_token is not None:
            self.analysis_token.cancel("paused in Studio")
            self.status.set_busy("Pausing analyzer safely…")

    def _configure_automation(self) -> None:
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._folder_changed)
        self.watch_debounce = QTimer(self)
        self.watch_debounce.setSingleShot(True)
        self.watch_debounce.setInterval(2400)
        self.watch_debounce.timeout.connect(self.scan_configured)
        self.periodic_scan = QTimer(self)
        self.periodic_scan.setInterval(10 * 60 * 1000)
        self.periodic_scan.timeout.connect(self.scan_configured)
        self._refresh_watched_paths()
        if self.auto_button.isChecked():
            self.periodic_scan.start()

    def _pause_automation(self) -> None:
        """Silence every timer that could reopen the library behind our back.

        ``engine.search()`` starts a closed engine on demand, so a folder
        watchdog or the ten-minute rescan firing mid-relocation would recreate
        the database file that is halfway through moving — or the one a reset
        has just deleted.
        """
        self.periodic_scan.stop()
        self.watch_debounce.stop()
        self.search_timer.stop()

    def _resume_automation(self) -> None:
        """Start watching again, against whatever folders the config now lists."""
        self._refresh_watched_paths()
        if self.auto_button.isChecked():
            self.periodic_scan.start()

    def _refresh_watched_paths(self) -> None:
        if not hasattr(self, "watcher"):
            return
        existing = self.watcher.directories()
        if existing:
            self.watcher.removePaths(existing)
        if self.auto_button.isChecked():
            paths = [str(path) for path in self.config.library.roots if path.is_dir()]
            if paths:
                self.watcher.addPaths(paths)

    def _folder_changed(self, _: str) -> None:
        if self.auto_button.isChecked() and not self.scan_running:
            self.status.set_ready("Change detected · indexing shortly")
            self.watch_debounce.start()

    def toggle_auto_sync(self) -> None:
        self._set_auto_sync(self.auto_button.isChecked(), notify=True)

    def _set_auto_sync(self, enabled: bool, *, notify: bool = False) -> None:
        self.settings.setValue("auto_sync", enabled)
        self.auto_button.setChecked(enabled)
        self.auto_button.setText("Auto sync on" if enabled else "Auto sync off")
        if enabled:
            self.periodic_scan.start()
            self._refresh_watched_paths()
            if notify:
                self.show_toast("Automatic indexing is on.")
        else:
            self.periodic_scan.stop()
            self.watch_debounce.stop()
            self._refresh_watched_paths()
            if notify:
                self.show_toast("Automatic indexing is off. Manual refresh still works.")

    # ── settings ──────────────────────────────────────────────────────────

    def open_settings(self) -> None:
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(
            self.settings,
            self.tokens,
            self,
            library_path=self.config.storage.db_path,
            usage=describe_storage(self.config).as_dict(),
        )
        dialog.saved.connect(self.apply_settings)
        dialog.preview_requested.connect(self.preview_settings)
        dialog.reset_requested.connect(self.reset_preferences)
        dialog.relocate_requested.connect(self.relocate_library)
        dialog.open_library_folder_requested.connect(self.open_library_folder)
        dialog.erase_everything_requested.connect(self.erase_everything)
        dialog.finished.connect(self._settings_closed)
        self.settings_dialog = dialog
        dialog.open()

    def _settings_closed(self, result: int) -> None:
        # A cancelled dialog must not leave a live preview applied.
        if result != QDialogAccepted:
            self.apply_tokens(palette(str(self.settings.value("palette", "Aurora"))))
        self.settings_dialog = None

    def preview_settings(self, values: dict[str, object]) -> None:
        """Apply appearance choices live while the dialog is still open."""
        for key in ("palette", "accent", "intensity", "density", "animations"):
            if key in values:
                self.settings.setValue(key, values[key])
        self.apply_tokens(palette(str(values.get("palette", "Aurora"))))

    def apply_settings(self, values: dict[str, object]) -> None:
        for key, value in values.items():
            self.settings.setValue(key, value)
        self.video_hover_enabled = bool(values.get("video_hover", True))
        self.auto_select_results = bool(values.get("auto_select", True))
        self.show_tags = bool(values.get("show_tags", True))
        self.card_size_name = str(values.get("card_size", "Comfortable"))
        self.card_width = CARD_WIDTHS.get(self.card_size_name, 245)
        self._set_auto_sync(bool(values.get("auto_sync", True)))
        self.apply_tokens(palette(str(values.get("palette", "Aurora"))))
        self.show_toast("Settings saved.")

    def reset_preferences(self, notify: bool = True) -> None:
        """Forget every appearance and behaviour choice. Data is untouched."""
        for key in (
            "palette", "accent", "intensity", "density", "card_size",
            "auto_sync", "video_hover", "auto_select", "animations", "show_tags",
        ):
            self.settings.remove(key)
        self.video_hover_enabled = True
        self.auto_select_results = True
        self.show_tags = True
        self.card_size_name = "Comfortable"
        self.card_width = CARD_WIDTHS["Comfortable"]
        self._set_auto_sync(True, notify=False)
        self.apply_tokens(TOKENS)
        if notify:
            self.show_toast(f"Preferences reset to the {TOKENS.name} palette.")

    def open_library_folder(self) -> None:
        """Show the folder holding the index and previews in the file manager."""
        home = self.config.storage.db_path.parent
        home.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(home)))

    def relocate_library(self, mode: str = "move") -> None:
        """Store the index somewhere else, or switch to a library kept there.

        ``move`` carries this library to a folder of the user's choosing.
        ``adopt`` leaves both libraries alone and starts using the one already
        stored at the chosen folder — which is how someone keeps a work library
        and a home library on the same machine.
        """
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        prompt = (
            "Choose a folder to keep your library in"
            if mode == "move"
            else "Choose a folder that already holds a library"
        )
        chosen = QFileDialog.getExistingDirectory(self, prompt, str(self.config.storage.db_path.parent))
        if not chosen:
            return
        # A bare drive or a documents folder would end up strewn with our
        # files; giving the library its own named folder is what users expect
        # from "move it to D:". An existing library is adopted where it is.
        target = Path(chosen).resolve()
        if mode == "move" and not (target / self.config.storage.db_path.name).is_file():
            target = target / "File Indexer Library"

        try:
            plan = plan_relocation(self.config, target, mode=cast(RelocationMode, mode))
        except ConfigError as exc:
            QMessageBox.warning(self, "Cannot use that folder", str(exc))
            return

        if mode == "move":
            question = (
                f"Studio will close the library, move {human_size(plan.bytes_to_move)} to:\n\n"
                f"{plan.destination}\n\nand reopen there. Nothing is re-scanned, and your "
                "original media files are not touched."
            )
        else:
            question = (
                f"Studio will switch to the library stored in:\n\n{plan.destination}\n\n"
                + (
                    "That library is opened as it is."
                    if plan.adopts_existing
                    else "There is no library there yet, so a new empty one is created."
                )
                + "\n\nYour current library stays where it is and is not deleted."
            )
        if QMessageBox.question(self, "Change library location?", question) != _YES:
            return

        self.status.set_busy("Moving your library…" if mode == "move" else "Switching library…")
        self._pause_automation()
        destination = plan.destination

        def work() -> dict[str, Any]:
            # The database must be closed before its file can move, and closed
            # anyway before we start reading a different one.
            self.engine.close()
            report = relocate_storage(self.config, destination, mode=cast(RelocationMode, mode))
            save_desktop_config(self.config)
            return report.as_dict()

        worker = EngineWorker(work)
        worker.signals.result.connect(self._library_moved)
        worker.signals.error.connect(self._relocation_failed)
        self._start_worker(worker)

    def _library_moved(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        self.engine = MediaEngine(self.config)
        self.status.set_ready("Library ready")
        self.show_toast(f"Library now stored in {result.get('destination', 'its new home')}.")
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.close()
        self._resume_automation()
        self._start_engine()

    def _relocation_failed(self, detail: str) -> None:
        """Reopen the library that never moved, then explain what happened."""
        self.engine = MediaEngine(self.config)
        self.status.set_ready("Library unchanged")
        self._resume_automation()
        self._start_engine()
        self._show_worker_error(detail)

    def erase_everything(self) -> None:
        """Delete the index, previews, logs and preferences, after a typed yes."""
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        usage = describe_storage(self.config)
        summary = (
            f"{human_size(usage.total_bytes)} of index and previews in {usage.home} will be "
            f"deleted, along with your {len(self.config.library.roots)} library folder(s), every "
            "tag and label, and all settings."
        )
        confirm = EraseConfirmDialog(summary, self.tokens, self)
        if confirm.exec() != QDialogAccepted:
            return
        self.status.set_busy("Erasing everything…")
        self._pause_automation()

        def work() -> dict[str, Any]:
            self.engine.close()
            report = master_reset(self.config)
            save_desktop_config(self.config)
            return report.as_dict()

        worker = EngineWorker(work)
        worker.signals.result.connect(self._erase_finished)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _erase_finished(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        # The new engine first: resetting preferences repaints, and a repaint
        # searches — against whichever engine is on `self` at that moment. The
        # old one would obligingly recreate the database we just deleted.
        self.engine = MediaEngine(self.config)
        self.reset_preferences(notify=False)
        self.current_query = ""
        self.current_filter = ""
        self.active_facets = []
        self.search.clear()
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.close()
        self.status.set_ready("Everything erased")
        freed = str(result.get("bytes_human") or "0 B")
        self.show_toast(f"Library erased — {freed} freed. Add a folder to start again.")
        kept = result.get("skipped")
        if isinstance(kept, list) and kept:
            QMessageBox.information(
                self,
                "Some files were kept",
                "Studio refused to delete these because they hold, or sit inside, your media "
                "folders:\n\n" + "\n".join(f"{item[0]} — {item[1]}" for item in kept[:6]),
            )
        self._resume_automation()
        self._start_engine()

    # ── analyzer store ────────────────────────────────────────────────────

    def open_analyzer_store(self) -> None:
        if self.analyzer_dialog is not None and self.analyzer_dialog.isVisible():
            self.analyzer_dialog.raise_()
            self.analyzer_dialog.activateWindow()
            return
        dialog = AnalyzerStoreDialog(self.tokens, self)
        dialog.toggle_requested.connect(self.toggle_analyzer)
        dialog.run_requested.connect(self.run_analyzer)
        dialog.configure_requested.connect(self.configure_analyzer)
        dialog.add_api_requested.connect(self.add_analyzer_api)
        dialog.lm_studio_requested.connect(self.configure_lm_studio)
        dialog.install_model_requested.connect(self.install_model)
        dialog.use_model_requested.connect(self.use_model)
        dialog.cancel_install_requested.connect(self.cancel_model_install)
        dialog.refresh_requested.connect(self.refresh_analyzer_catalog)
        dialog.open_pack_folder_requested.connect(self.open_pack_folder)
        dialog.install_pack_requested.connect(self.install_filter_pack)
        dialog.uninstall_pack_requested.connect(self.uninstall_filter_pack)
        dialog.choose_vision_model_requested.connect(self.choose_vision_model)
        dialog.open_model_page_requested.connect(self.open_model_page)
        dialog.finished.connect(lambda _: setattr(self, "analyzer_dialog", None))
        self.analyzer_dialog = dialog
        dialog.open()
        self.refresh_analyzer_catalog()

    def refresh_analyzer_catalog(self) -> None:
        if self.analyzer_dialog is None:
            return

        def load() -> dict[str, Any]:
            from ..plugins import PluginManager

            manager = PluginManager(self.engine)
            return {
                "catalog": manager.catalog(),
                "models": manager.model_store(),
                "packs": manager.filter_packs(),
                "vision": manager.vision_models(),
            }

        worker = EngineWorker(load)
        worker.signals.result.connect(self._show_analyzer_catalog)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _show_analyzer_catalog(self, payload: object) -> None:
        if self.analyzer_dialog is None or not isinstance(payload, dict):
            return
        catalog = payload.get("catalog")
        if isinstance(catalog, list):
            self.analyzer_dialog.set_catalog(cast(list[dict[str, object]], catalog))
        models = payload.get("models")
        if isinstance(models, dict):
            self.analyzer_dialog.set_model_state(models)
        packs = payload.get("packs")
        if isinstance(packs, dict):
            self.analyzer_dialog.set_pack_state(packs)
        vision = payload.get("vision")
        if isinstance(vision, dict):
            self.analyzer_dialog.set_vision_state(vision)

    def open_pack_folder(self) -> None:
        """Reveal the folder where a user's own filter packs live."""
        from ..filters import BUILTIN_DIR, user_pack_dir

        target = user_pack_dir(self.config)
        target.mkdir(parents=True, exist_ok=True)
        readme = target / "README.txt"
        if not readme.exists():
            readme.write_text(
                "Drop *.toml filter packs here to add your own search filters.\n\n"
                "Every pack names a namespace and a closed list of labels; each label\n"
                "becomes a facet value and a search filter. Copy one of the shipped\n"
                "packs as a starting point:\n\n"
                f"  {BUILTIN_DIR}\n\n"
                "Studio picks up changes the next time the analyzer store is refreshed.\n",
                encoding="utf-8",
            )
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def install_filter_pack(self) -> None:
        """Add a filter pack from a ``.toml`` file someone else wrote."""
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose a filter pack", str(Path.home()), "Filter packs (*.toml)"
        )
        if not chosen:
            return

        def work() -> dict[str, Any]:
            from ..plugins import PluginManager

            return PluginManager(self.engine).install_filter_pack(chosen)

        worker = EngineWorker(work)
        worker.signals.result.connect(self._filter_pack_installed)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _filter_pack_installed(self, payload: object) -> None:
        summary = payload if isinstance(payload, dict) else {}
        name = str(summary.get("name") or summary.get("pack_id") or "Filter")
        self.show_toast(f"{name} installed — press Add filter to switch it on.")
        self.refresh_analyzer_catalog()

    def uninstall_filter_pack(self, row: dict[str, object]) -> None:
        """Delete a filter pack the user installed. Its tags are kept."""
        pack_id = str(row.get("pack_id") or row.get("plugin_id") or "")
        name = str(row.get("name") or pack_id)
        answer = QMessageBox.question(
            self,
            "Delete this filter?",
            f"“{name}” will be removed from the store. Tags it already applied stay on your "
            "files, and reinstalling it picks them straight back up.",
        )
        if answer != _YES:
            return

        def work() -> dict[str, Any]:
            from ..plugins import PluginManager

            return PluginManager(self.engine).remove_filter_pack(pack_id)

        worker = EngineWorker(work)
        worker.signals.result.connect(lambda _: self._filter_pack_removed(name))
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _filter_pack_removed(self, name: str) -> None:
        self.show_toast(f"{name} deleted.")
        self.refresh_analyzer_catalog()
        self.refresh_facets()

    def open_model_page(self, url: str) -> None:
        """Open a model's own repository in the browser."""
        if url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(url))

    def choose_vision_model(self) -> None:
        """Adopt a downloaded ONNX tagging model for local, offline tagging."""
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose a tagging model", str(Path.home()), "ONNX models (*.onnx)"
        )
        if not chosen:
            return
        model = Path(chosen)
        labels = ""
        # A model's scores are meaningless without its label list, and the two
        # are shipped together often enough to try before asking.
        if not any(
            model.with_name(name).is_file()
            for name in ("selected_tags.csv", "labels.txt", "classes.txt", "labels.json")
        ):
            picked, _also = QFileDialog.getOpenFileName(
                self,
                f"Choose the label list for {model.name}",
                str(model.parent),
                "Label lists (*.csv *.txt *.json)",
            )
            if not picked:
                self.show_toast("A tagging model needs its label list. Nothing changed.")
                return
            labels = picked

        def work() -> dict[str, Any]:
            from ..plugins import PluginManager

            return PluginManager(self.engine).use_vision_model(str(model), labels_path=labels)

        worker = EngineWorker(work)
        worker.signals.result.connect(lambda _: self._vision_model_chosen(model.name))
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _vision_model_chosen(self, name: str) -> None:
        self.show_toast(f"Tagging with {name}. Run it from the Analyzers tab.")
        self.refresh_analyzer_catalog()

    def toggle_analyzer(self, row: dict[str, object]) -> None:
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        plugin_id = str(row.get("plugin_id") or "")
        enable = not bool(row.get("enabled"))
        grant_network = False
        plugin = self.engine.plugins.get(plugin_id)
        if (
            enable
            and plugin is not None
            and plugin.info.network
            and not self.config.plugins.allow_network
        ):
            answer = QMessageBox.question(
                self,
                "Allow analyzer network access?",
                f"{plugin_id} declares network access. This is required for LM Studio and HTTP "
                "analyzers. Original files stay read-only. Continue?",
            )
            grant_network = answer == QMessageBox.StandardButton.Yes
            if not grant_network:
                return

        def work() -> dict[str, object]:
            from ..plugins import PluginManager

            return PluginManager(self.engine).set_enabled(
                plugin_id, enable, grant_network=grant_network
            )

        worker = EngineWorker(work)
        worker.signals.result.connect(lambda _: self._analyzer_changed(plugin_id, enable))
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _analyzer_changed(self, plugin_id: str, enabled: bool) -> None:
        self.show_toast(f"{plugin_id} {'added' if enabled else 'removed'}.")
        self.refresh_analyzer_catalog()
        self.refresh_facets()

    def run_analyzer(self, row: dict[str, object]) -> None:
        if self.scan_running or self.analysis_running:
            self.show_toast("A background job is already running.")
            return
        plugin_id = str(row.get("plugin_id") or "")
        media_types = _as_str_list(row.get("media_types"))
        retry_failed = bool(row.get("retry_failed"))
        scope = str(row.get("scope_label") or "Everything")
        self.analysis_running = True
        self.analysis_token = CancelToken()
        token = self.analysis_token
        self.status.set_busy(f"Running {plugin_id} · {scope.lower()}…")

        def progress(event: ProgressEvent) -> None:
            worker.signals.progress.emit(event)

        def work() -> object:
            return self.engine.backfill(
                [plugin_id],
                media_types=media_types or None,
                retry_failed=retry_failed,
                progress=progress,
                cancel=token,
            )

        worker = EngineWorker(work)
        worker.signals.progress.connect(self._job_progress)
        worker.signals.result.connect(self._analyzer_finished)
        worker.signals.error.connect(self._job_failed)
        worker.signals.finished.connect(self._analyzer_stopped)
        self._start_worker(worker)

    def _analyzer_finished(self, payload: object) -> None:
        completed = int(getattr(payload, "completed", 0))
        failed = int(getattr(payload, "failed", 0))
        self.status.set_ready("Analyzer complete")
        detail = f"{completed:,} items enriched"
        if failed:
            detail += f", {failed:,} failed — open the store to retry them"
        self.show_toast(f"Analyzer complete — {detail}.")
        self.refresh_library()
        self.refresh_facets()
        self.refresh_analyzer_catalog()

    def _analyzer_stopped(self) -> None:
        self.analysis_running = False
        self.analysis_token = None

    def configure_analyzer(self, row: dict[str, object]) -> None:
        if str(row.get("plugin_id") or "") == LM_STUDIO_ID:
            self.configure_lm_studio()
        else:
            QMessageBox.information(
                self,
                "Analyzer configuration",
                "This analyzer uses its registered API settings. Add a new endpoint if you need "
                "a different configuration.",
            )

    # ── assistant ─────────────────────────────────────────────────────────

    def open_assistant(self) -> None:
        """Open the chat window, creating the conversation on first use."""
        if self.assistant_dialog is not None and self.assistant_dialog.isVisible():
            self.assistant_dialog.raise_()
            self.assistant_dialog.activateWindow()
            return
        if self.assistant is None:
            self.assistant = Assistant(self.engine)
        dialog = AssistantDialog(self.assistant, self.tokens, self)
        dialog.question_started.connect(self.ask_assistant)
        dialog.approve_requested.connect(self.approve_assistant_change)
        dialog.reject_requested.connect(self.reject_assistant_change)
        dialog.finished.connect(lambda _: setattr(self, "assistant_dialog", None))
        self.assistant_dialog = dialog
        dialog.open()

    def ask_assistant(self, question: str) -> None:
        """Run one question off the GUI thread, streaming events back to it."""
        assistant = self.assistant
        dialog = self.assistant_dialog
        if assistant is None or dialog is None:
            return

        def work() -> None:
            for item in assistant.ask(question):
                worker.signals.progress.emit(item)

        worker = EngineWorker(work)
        worker.signals.progress.connect(self._assistant_event)
        worker.signals.error.connect(self._assistant_crashed)
        worker.signals.finished.connect(self._assistant_done)
        self._start_worker(worker)

    def _assistant_event(self, payload: object) -> None:
        if self.assistant_dialog is not None and isinstance(payload, AssistantEvent):
            self.assistant_dialog.handle_event(payload)

    def _assistant_crashed(self, detail: str) -> None:
        summary = detail.strip().splitlines()[-1] if detail.strip() else "Unknown error"
        if self.assistant_dialog is not None:
            self.assistant_dialog.handle_event(AssistantEvent(kind="error", text=summary[:300]))

    def _assistant_done(self) -> None:
        if self.assistant_dialog is not None:
            self.assistant_dialog.finish()

    def approve_assistant_change(self, token: str) -> None:
        """Apply a staged change, then refresh everything it could have moved."""
        assistant = self.assistant
        if assistant is None:
            return

        def work() -> dict[str, Any]:
            return dict(assistant.approve(token))

        worker = EngineWorker(work)
        worker.signals.result.connect(partial(self._assistant_change_applied, token))
        worker.signals.error.connect(partial(self._assistant_change_failed, token))
        self._start_worker(worker)

    def _assistant_change_applied(self, token: str, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        detail = ", ".join(f"{key}: {value}" for key, value in list(result.items())[:3])
        if self.assistant_dialog is not None:
            self.assistant_dialog.change_applied(token, f"Applied — {detail}" if detail else "Applied.")
        self.show_toast("Assistant change applied.")
        self.refresh_library()
        self.refresh_facets()
        self.refresh_analyzer_catalog()

    def _assistant_change_failed(self, token: str, detail: str) -> None:
        summary = detail.strip().splitlines()[-1] if detail.strip() else "Unknown error"
        if self.assistant_dialog is not None:
            self.assistant_dialog.change_failed(token, summary)

    def reject_assistant_change(self, token: str) -> None:
        if self.assistant is not None:
            self.assistant.reject(token)

    # ── model store ───────────────────────────────────────────────────────

    def install_model(self, key: str) -> None:
        """Download a model through LM Studio, reporting progress as it goes."""
        if self.installing_model:
            self.show_toast(f"Already installing {self.installing_model}.")
            return
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        answer = QMessageBox.question(
            self,
            "Download this model?",
            f"Studio will ask LM Studio to download:\n\n{key}\n\n"
            "Models are several gigabytes and the download uses your internet connection. "
            "You can cancel at any time; LM Studio keeps partial files and resumes.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.installing_model = key
        self.status.set_busy(f"Downloading {key}…")

        def work() -> dict[str, Any]:
            from ..plugins import ModelLibrary

            library = ModelLibrary()
            self._model_library = library

            def report(fraction: float | None, message: str) -> None:
                worker.signals.progress.emit((key, fraction, message))

            library.install(key, on_progress=report)
            return {"key": key}

        worker = EngineWorker(work)
        worker.signals.progress.connect(self._model_progress)
        worker.signals.result.connect(self._model_installed)
        worker.signals.error.connect(self._model_failed)
        worker.signals.finished.connect(lambda: setattr(self, "installing_model", ""))
        self._start_worker(worker)

    def _model_progress(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            return
        key, fraction, message = payload
        value = float(fraction) if isinstance(fraction, (int, float)) else None
        self.status.set_busy(f"Downloading {key} — {message}"[:110], value)
        if self.analyzer_dialog is not None:
            self.analyzer_dialog.show_install_progress(str(key), value, str(message))

    def _model_installed(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        key = str(result.get("key") or "")
        self.status.set_ready("Model installed")
        self.refresh_analyzer_catalog()
        answer = QMessageBox.question(
            self,
            "Use this model for tagging?",
            f"{key} is installed. Use it to categorize your videos and photos now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.use_model({"key": key, "vision": True})

    def _model_failed(self, detail: str) -> None:
        self.status.set_ready("Model download needs attention")
        self._show_worker_error(detail)
        self.refresh_analyzer_catalog()

    def cancel_model_install(self) -> None:
        library = getattr(self, "_model_library", None)
        if library is not None:
            library.cancel_install()
            self.status.set_ready("Download cancelled")
            self.show_toast("Download cancelled. LM Studio keeps what it already fetched.")

    def use_model(self, row: dict[str, Any]) -> None:
        """Point the analyzer at a model and offer to tag videos immediately."""
        key = str(row.get("key") or "")
        if not key:
            return
        vision = bool(row.get("vision", True))
        self.status.set_busy(f"Loading {key}…")

        def work() -> dict[str, Any]:
            from ..plugins import ModelLibrary, PluginManager

            manager = PluginManager(self.engine)
            outcome = manager.use_model_for_tagging(key, vision=vision)
            try:
                ModelLibrary().load(key)
            except Exception:  # noqa: BLE001 - a cold start is slow, not broken
                pass
            return outcome

        worker = EngineWorker(work)
        worker.signals.result.connect(self._model_selected)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _model_selected(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        key = str(result.get("model") or "")
        vision = bool(result.get("vision"))
        self.status.set_ready("Model ready")
        self.refresh_analyzer_catalog()
        if not vision:
            self.show_toast(f"{key} is now tagging. It reads metadata only — it cannot see video.")
            return
        answer = QMessageBox.question(
            self,
            "Categorize your videos now?",
            f"{key} is ready and can see video keyframes.\n\n"
            "Start categorizing videos in the background? You can cancel at any time and "
            "resume later — finished work is never repeated.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.run_analyzer(
                {
                    "plugin_id": LM_STUDIO_ID,
                    "media_types": ["video"],
                    "scope_label": "Videos only",
                    "retry_failed": True,
                }
            )
        else:
            self.show_toast(f"{key} is now tagging new media.")

    def add_analyzer_api(self) -> None:
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        base_url, accepted = QInputDialog.getText(
            self,
            "Add analyzer API",
            "Base URL (must provide /manifest, /health, and /analyze):",
            text="http://127.0.0.1:9000",
        )
        if not accepted or not base_url.strip():
            return
        token, accepted = QInputDialog.getText(
            self,
            "Optional API token",
            "Bearer token (leave blank for an unauthenticated local service):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        from ..plugins import endpoint_is_local

        allow_external = False
        if not endpoint_is_local(base_url):
            answer = QMessageBox.question(
                self,
                "External analyzer endpoint",
                "This endpoint can receive media metadata or generated previews on another "
                "computer. Register it?",
            )
            allow_external = answer == QMessageBox.StandardButton.Yes
            if not allow_external:
                return

        def work() -> dict[str, object]:
            from ..plugins import PluginManager

            return (
                PluginManager(self.engine)
                .register_remote(
                    base_url.strip(), auth_token=token or None, allow_external=allow_external
                )
                .as_dict()
            )

        worker = EngineWorker(work)
        worker.signals.result.connect(self._api_registered)
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _api_registered(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        self.show_toast(f"Added analyzer {result.get('plugin_id', '')}.")
        self.refresh_analyzer_catalog()

    def configure_lm_studio(self) -> None:
        if self.scan_running or self.analysis_running:
            self.show_toast("Wait for the current background job first.")
            return
        current = self.config.plugin_config(LM_STUDIO_ID)
        from ..plugins.builtin.lm_studio import discover_base_url

        configured = str(current.get("base_url") or "http://127.0.0.1:1234")
        # LM Studio reuses whichever port it was last started on, so the
        # configured one goes stale without anything having been changed.
        # Offer what is actually listening rather than what was true once.
        detected = discover_base_url(configured)
        base_url, accepted = QInputDialog.getText(
            self,
            "LM Studio server",
            "LM Studio server URL:"
            + ("" if not detected else f"\n\nDetected a server at {detected}"),
            text=detected or configured,
        )
        if not accepted or not base_url.strip():
            return
        frames, accepted = QInputDialog.getInt(
            self,
            "Video keyframes",
            "How many frames from each video should the model look at?\n"
            "More frames describe long clips better and cost more time per file.",
            int(current.get("frame_count", 4) or 4),
            1,
            8,
        )
        if not accepted:
            return
        token, accepted = QInputDialog.getText(
            self,
            "LM Studio token",
            "Optional API token (blank keeps the current token):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        values = dict(current)
        values.update(
            {
                "base_url": base_url.strip(),
                "frame_count": int(frames),
                "structured_output": True,
            }
        )
        if token:
            values["api_token"] = token

        def work() -> None:
            from ..plugins import PluginManager

            manager = PluginManager(self.engine)
            manager.configure(LM_STUDIO_ID, values)
            manager.set_enabled(LM_STUDIO_ID, True, grant_network=True)

        worker = EngineWorker(work)
        worker.signals.result.connect(lambda _: self._lm_studio_connected())
        worker.signals.error.connect(self._show_worker_error)
        self._start_worker(worker)

    def _lm_studio_connected(self) -> None:
        self.show_toast("LM Studio settings saved.")
        self.refresh_analyzer_catalog()

    # ── window behavior ───────────────────────────────────────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = self.width() < 1180
        self.inspector.setVisible(not compact)
        if not self._reflow_pending:
            self._reflow_pending = True
            QTimer.singleShot(30, self._finish_reflow)
        if hasattr(self, "toast") and self.toast.isVisible():
            self._position_toast()

    def _finish_reflow(self) -> None:
        self._reflow_pending = False
        self._reflow_cards()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(Path(url.toLocalFile()).is_dir() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        folders = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if Path(url.toLocalFile()).is_dir()
        ]
        if folders:
            self.add_folders(folders)
            event.acceptProposedAction()

    def show_toast(self, message: str) -> None:
        self.toast.setText(message)
        self.toast.adjustSize()
        self._position_toast()
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(3800)

    def _position_toast(self) -> None:
        central = self.centralWidget()
        x = max(12, (central.width() - self.toast.width()) // 2)
        y = max(12, central.height() - self.toast.height() - 30)
        self.toast.move(QPoint(x, y))

    def _hide_toast(self) -> None:
        self.toast.hide()

    def _show_worker_error(self, detail: str) -> None:
        summary = detail.strip().splitlines()[-1] if detail.strip() else "Unknown error"
        self.status.set_ready("Something needs attention")
        self.show_toast(summary[:200])

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.scan_token is not None:
            self.scan_token.cancel("Studio is closing")
        if self.analysis_token is not None:
            self.analysis_token.cancel("Studio is closing")
        library = getattr(self, "_model_library", None)
        if library is not None:
            library.cancel_install()
        self.thread_pool.waitForDone(2200)
        try:
            self.engine.close()
        finally:
            event.accept()


#: ``QDialog.Accepted`` as a plain int, so the settings handler does not have
#: to import QDialog purely to compare one result code.
QDialogAccepted = 1

#: ``QMessageBox.question`` returns this when the user agrees. Named because
#: the comparison reads as noise inline, three times over.
_YES = QMessageBox.StandardButton.Yes


class _Sidebar(QFrame):
    """Navigation rail, painted with the palette's vertical brand gradient."""

    def __init__(self, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setObjectName("StudioSidebar")
        self.setFixedWidth(238)

    def paintEvent(self, event: Any) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter, QPen

        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.fillRect(rect, self.tokens.sidebar_gradient(rect))
        painter.setPen(QPen(self.tokens.color(self.tokens.border), 1))
        painter.drawLine(rect.topRight(), rect.bottomRight())


class _SoftPanel(QFrame):
    """A muted inset panel; the sidebar's automation card sits in one."""

    def __init__(self, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens

    def paintEvent(self, event: Any) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.tokens.color(self.tokens.surface_soft))
        painter.drawRoundedRect(rect, self.tokens.radius_md, self.tokens.radius_md)


class _BrandMark(QLabel):
    """The app mark, filled with the same gradient as primary buttons."""

    def __init__(self, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setFixedSize(38, 38)

    def paintEvent(self, event: Any) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.tokens.brand_gradient(rect, hover=0.5))
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(self.tokens.color(self.tokens.on_brand))
        font = QFont(self.font())
        font.setPixelSize(19)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "✦")


def build_application(
    config: Config, *, engine: MediaEngine | None = None
) -> tuple[QApplication, StudioWindow]:
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv)
    app.setApplicationName("File Indexer Studio")
    app.setOrganizationName("File Indexer")
    app.setStyle("Fusion")
    settings = QSettings("File Indexer", "Studio")
    chosen = palette(str(settings.value("palette", "Aurora")))
    window = StudioWindow(config, engine=engine, tokens=chosen)
    app.setStyleSheet(application_stylesheet(window.tokens))
    return app, window
