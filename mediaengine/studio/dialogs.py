"""Settings, folder management, and analyzer/model surfaces for Studio."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..maintenance import is_temporary_location
from .components import AnimatedButton, CheckBox, human_size
from .tokens import AUTO_PALETTE, PALETTES, TOKENS, StudioTokens, palette, palette_choices

__all__ = [
    "AnalyzerStoreDialog",
    "VisionModelCard",
    "EraseConfirmDialog",
    "FolderManagerDialog",
    "SettingsDialog",
    "clear_layout",
    "is_temporary_location",
]

#: What a scoped analyzer run maps to, in the order the picker shows them.
RUN_SCOPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Videos only", ("video",)),
    ("Photos only", ("image",)),
    ("Videos and photos", ("video", "image")),
    ("Documents only", ("document",)),
    ("Everything", ()),
)


def clear_layout(layout: QLayout) -> None:
    """Remove and schedule deletion of every widget in ``layout``."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _heading(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    font = QFont(label.font())
    font.setPointSize(18)
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    return label


def _copy(text: str, tokens: StudioTokens, parent: QWidget, *, size: int = 12) -> QLabel:
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{tokens.ink_soft}; font-size:{size}px;")
    return label


def _section(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setStyleSheet("font-size:14px; font-weight:650;")
    return label


class Panel(QFrame):
    """A gradient card used as the container for one group of controls."""

    def __init__(self, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(11)

    def paintEvent(self, event: Any) -> None:
        from PySide6.QtCore import QRectF

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self.tokens.radius_lg)
        painter.setBrush(self.tokens.surface_gradient(rect))
        painter.setPen(QPen(QColor(self.tokens.border), 1))
        painter.drawRoundedRect(rect, radius, radius)
        super().paintEvent(event)


class PalettePreview(QWidget):
    """A live swatch of the palette currently selected in Settings."""

    def __init__(self, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setFixedHeight(88)

    def set_tokens(self, tokens: StudioTokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, event: Any) -> None:
        from PySide6.QtCore import QRectF

        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.setClipRect(rect)
        painter.fillRect(rect, self.tokens.canvas_gradient(rect))
        swatches = [
            self.tokens.surface,
            self.tokens.brand,
            self.tokens.accent,
            self.tokens.mint,
            self.tokens.coral,
        ]
        gap = 8.0
        width = (rect.width() - 24 - gap * (len(swatches) - 1)) / len(swatches)
        for index, color in enumerate(swatches):
            left = 12 + index * (width + gap)
            chip = QRectF(left, 14, width, rect.height() - 28)
            if index == 1:
                painter.setBrush(self.tokens.brand_gradient(chip))
            else:
                painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor(self.tokens.border), 1))
            painter.drawRoundedRect(chip, 12, 12)


class SettingsDialog(QDialog):
    """Appearance, behavior, library location, and both kinds of reset."""

    saved = Signal(dict)
    preview_requested = Signal(dict)
    reset_requested = Signal()
    #: Carries the relocation mode: ``move`` the current library, or ``adopt``
    #: one that already exists somewhere else.
    relocate_requested = Signal(str)
    open_library_folder_requested = Signal()
    erase_everything_requested = Signal()

    def __init__(
        self,
        settings: Any,
        tokens: StudioTokens = TOKENS,
        parent: QWidget | None = None,
        *,
        library_path: Path | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tokens = tokens
        self.usage = usage or {}
        self.library_path = library_path or (
            Path(str(self.usage["db_path"])) if self.usage.get("db_path") else None
        )
        self.accent = str(settings.value("accent", "") or "")
        self.setWindowTitle("Studio Settings")
        self.resize(760, 640)
        self.setMinimumSize(660, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(_heading("Settings", self))
        outer.addWidget(
            _copy("Personalize Studio, tune automation, and see where your library lives.", tokens, self)
        )

        tabs = QTabWidget(self)
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._behavior_tab(), "Behavior")
        tabs.addTab(self._library_tab(), "Library")
        tabs.addTab(self._reset_tab(), "Reset")
        outer.addWidget(tabs, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = AnimatedButton("Cancel", variant="ghost", tokens=tokens, parent=self)
        cancel.clicked.connect(self.reject)
        save = AnimatedButton("Save settings", variant="primary", tokens=tokens, parent=self)
        save.clicked.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        outer.addLayout(actions)

    # ── tabs ────────────────────────────────────────────────────────────────

    def _scrollable(self, panel: QWidget) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        area = QScrollArea(tab)
        area.setWidgetResizable(True)
        area.setWidget(panel)
        layout.addWidget(area)
        return tab

    def _appearance_tab(self) -> QWidget:
        panel = Panel(self.tokens)
        body = panel.body
        body.addWidget(_section("Color palette", panel))
        body.addWidget(
            _copy(
                f"Palettes apply the moment you pick one — nothing needs restarting. "
                f"“{AUTO_PALETTE}” follows the light and dark setting of your desktop.",
                self.tokens,
                panel,
            )
        )
        self.palette_combo = QComboBox(panel)
        self.palette_combo.addItems(palette_choices())
        current = str(self.settings.value("palette", self.tokens.name))
        self.palette_combo.setCurrentText(
            current if current in PALETTES or current == AUTO_PALETTE else "Aurora"
        )
        self.palette_combo.currentTextChanged.connect(self._appearance_changed)
        body.addWidget(self.palette_combo)
        self.palette_preview = PalettePreview(self._preview_tokens(), panel)
        body.addWidget(self.palette_preview)

        accent_row = QHBoxLayout()
        self.accent_button = AnimatedButton(
            "Choose accent colour", variant="secondary", icon_text="◐", tokens=self.tokens, parent=panel
        )
        self.accent_button.clicked.connect(self._pick_accent)
        accent_row.addWidget(self.accent_button, 1)
        self.accent_clear = AnimatedButton("Use palette default", variant="ghost", tokens=self.tokens, parent=panel)
        self.accent_clear.clicked.connect(self._clear_accent)
        accent_row.addWidget(self.accent_clear)
        body.addLayout(accent_row)
        self.accent_note = _copy(self._accent_text(), self.tokens, panel, size=11)
        body.addWidget(self.accent_note)

        body.addSpacing(6)
        body.addWidget(_section("Gradient intensity", panel))
        body.addWidget(
            _copy(
                "How strongly Studio paints its background wash, button gradients, and hover glows. "
                "Slide to zero for flat, high-contrast surfaces.",
                self.tokens,
                panel,
            )
        )
        self.intensity = self._slider(panel, int(float(self.settings.value("intensity", 1.0)) * 100))
        body.addWidget(self.intensity)

        body.addWidget(_section("Corner softness", panel))
        self.density = self._slider(panel, int(float(self.settings.value("density", 1.0)) * 100), low=0, high=140)
        body.addWidget(self.density)

        body.addWidget(_section("Tile size", panel))
        self.card_size = QComboBox(panel)
        self.card_size.addItems(["Compact", "Comfortable", "Spacious", "Cinema"])
        self.card_size.setCurrentText(str(self.settings.value("card_size", "Comfortable")))
        self.card_size.currentTextChanged.connect(self._appearance_changed)
        body.addWidget(self.card_size)
        body.addStretch(1)
        return self._scrollable(panel)

    def _slider(self, parent: QWidget, value: int, *, low: int = 0, high: int = 100) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal, parent)
        slider.setRange(low, high)
        slider.setValue(max(low, min(high, value)))
        slider.setSingleStep(5)
        slider.setPageStep(20)
        slider.valueChanged.connect(self._appearance_changed)
        return slider

    def _behavior_tab(self) -> QWidget:
        panel = Panel(self.tokens)
        body = panel.body
        body.addWidget(_section("Automatic experience", panel))
        self.auto_sync = CheckBox(
            "Watch library folders and index changes automatically", self.tokens, panel
        )
        self.auto_sync.setChecked(bool(self.settings.value("auto_sync", True, type=bool)))
        self.video_hover = CheckBox("Play short, silent video previews on hover", self.tokens, panel)
        self.video_hover.setChecked(bool(self.settings.value("video_hover", True, type=bool)))
        self.auto_select = CheckBox(
            "Automatically open the first search result in Quick Look", self.tokens, panel
        )
        self.auto_select.setChecked(bool(self.settings.value("auto_select", True, type=bool)))
        self.animations = CheckBox(
            "Use interface animations and pointer-responsive tile motion", self.tokens, panel
        )
        self.animations.setChecked(bool(self.settings.value("animations", True, type=bool)))
        self.show_tags = CheckBox("Show analyzer tags on tiles", self.tokens, panel)
        self.show_tags.setChecked(bool(self.settings.value("show_tags", True, type=bool)))
        for check in (self.auto_sync, self.video_hover, self.auto_select, self.animations, self.show_tags):
            body.addWidget(check)
        body.addWidget(
            _copy(
                "Turning animations off keeps keyboard focus and state changes while removing "
                "decorative movement.",
                self.tokens,
                panel,
            )
        )
        body.addStretch(1)
        return self._scrollable(panel)

    def _library_tab(self) -> QWidget:
        panel = Panel(self.tokens)
        body = panel.body
        body.addWidget(_section("Where your library is stored", panel))
        home = str(self.usage.get("home") or "")
        location = home or (str(self.library_path.parent) if self.library_path else "Not started yet")
        path_label = QLabel(location, panel)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setStyleSheet(f"color:{self.tokens.ink}; font-size:12px; font-weight:600;")
        body.addWidget(path_label)
        body.addWidget(_copy(self._usage_text(), self.tokens, panel, size=11))

        if self.library_path is not None and is_temporary_location(self.library_path):
            warning = QLabel(
                "⚠  This index lives in a temporary folder. Windows can delete it at any time, "
                "which would force a full re-scan. Move it somewhere permanent.",
                panel,
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color:{self.tokens.danger}; font-size:12px; font-weight:600;")
            body.addWidget(warning)

        body.addSpacing(4)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        move = AnimatedButton(
            "Move library…", variant="primary", icon_text="→", tokens=self.tokens, parent=panel
        )
        move.setToolTip("Choose a new folder; the index and previews are moved there")
        move.clicked.connect(lambda: self.relocate_requested.emit("move"))
        actions.addWidget(move)
        switch = AnimatedButton(
            "Use another library…", variant="secondary", tokens=self.tokens, parent=panel
        )
        switch.setToolTip("Point Studio at a library folder that already exists, moving nothing")
        switch.clicked.connect(lambda: self.relocate_requested.emit("adopt"))
        actions.addWidget(switch)
        reveal = AnimatedButton("Open folder", variant="ghost", icon_text="▰", tokens=self.tokens, parent=panel)
        reveal.clicked.connect(self.open_library_folder_requested)
        actions.addWidget(reveal)
        actions.addStretch(1)
        body.addLayout(actions)

        body.addWidget(
            _copy(
                "Moving takes the index, the preview cache and the log with it, and Studio "
                "reopens in the new place — nothing is re-scanned. "
                "“Use another library” switches to a second library without touching either one. "
                "Only the index and generated previews live here; your original files are never "
                "moved, modified, or deleted.",
                self.tokens,
                panel,
            )
        )
        body.addStretch(1)
        return self._scrollable(panel)

    def _usage_text(self) -> str:
        """One line naming what is actually stored, so "move" has a size."""
        if not self.usage:
            return "Nothing has been indexed yet."
        parts = [
            f"Index {human_size(self.usage.get('db_bytes', 0))}",
            f"previews {human_size(self.usage.get('derivatives_bytes', 0))}",
        ]
        free = self.usage.get("free_bytes")
        if isinstance(free, int):
            parts.append(f"{human_size(free)} free on this drive")
        return "  ·  ".join(parts)

    def _reset_tab(self) -> QWidget:
        panel = Panel(self.tokens)
        body = panel.body
        body.addWidget(_section("Reset Studio preferences", panel))
        body.addWidget(
            _copy(
                "Restore the default palette, comfortable tiles, animations, and automatic "
                "behaviors. Your indexed library and original files are not deleted.",
                self.tokens,
                panel,
            )
        )
        reset = AnimatedButton(
            "Reset preferences", variant="secondary", tokens=self.tokens, parent=panel
        )
        reset.clicked.connect(self._confirm_reset)
        body.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)

        body.addSpacing(18)
        body.addWidget(_section("Erase everything", panel))
        danger = QLabel(
            "Deletes the index itself — every tag, label, person, favourite and preview, plus "
            "your folder list and all settings. Studio restarts empty, as if freshly installed.\n\n"
            "Your original photos, videos and documents are not touched. Nothing else can be "
            "recovered.",
            panel,
        )
        danger.setWordWrap(True)
        danger.setStyleSheet(f"color:{self.tokens.ink_soft}; font-size:12px;")
        body.addWidget(danger)
        erase = AnimatedButton(
            "Erase library and settings", variant="danger", icon_text="⚠", tokens=self.tokens, parent=panel
        )
        erase.clicked.connect(self.erase_everything_requested)
        body.addWidget(erase, 0, Qt.AlignmentFlag.AlignLeft)
        body.addStretch(1)
        return self._scrollable(panel)

    # ── appearance plumbing ─────────────────────────────────────────────────

    def _accent_text(self) -> str:
        if not self.accent:
            return "Using the palette's own brand colour."
        return f"Custom accent {self.accent.upper()} — Studio keeps it readable by adjusting lightness."

    def _preview_tokens(self) -> StudioTokens:
        # `palette()` rather than a dictionary lookup, so the swatch shows what
        # "Match Windows" resolves to right now instead of falling back to the
        # default palette.
        tokens = palette(self.palette_combo.currentText())
        if self.accent:
            tokens = tokens.with_accent(self.accent)
        if hasattr(self, "intensity"):
            tokens = tokens.with_intensity(self.intensity.value() / 100)
        if hasattr(self, "density"):
            tokens = tokens.with_density(self.density.value() / 100)
        return tokens

    def _pick_accent(self) -> None:
        start = QColor(self.accent) if self.accent else QColor(self._preview_tokens().brand)
        chosen = QColorDialog.getColor(start, self, "Choose an accent colour")
        if chosen.isValid():
            self.accent = chosen.name()
            self.accent_note.setText(self._accent_text())
            self._appearance_changed()

    def _clear_accent(self) -> None:
        self.accent = ""
        self.accent_note.setText(self._accent_text())
        self._appearance_changed()

    def _appearance_changed(self) -> None:
        self.palette_preview.set_tokens(self._preview_tokens())
        self.preview_requested.emit(self._values())

    def _values(self) -> dict[str, object]:
        return {
            "palette": self.palette_combo.currentText(),
            "accent": self.accent,
            "intensity": self.intensity.value() / 100,
            "density": self.density.value() / 100,
            "card_size": self.card_size.currentText(),
            "auto_sync": self.auto_sync.isChecked(),
            "video_hover": self.video_hover.isChecked(),
            "auto_select": self.auto_select.isChecked(),
            "animations": self.animations.isChecked(),
            "show_tags": self.show_tags.isChecked(),
        }

    def _save(self) -> None:
        self.saved.emit(self._values())
        self.accept()

    def _confirm_reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset Studio preferences?",
            "This resets appearance and behavior only. Your folders, index, labels, and "
            "original files stay intact.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.reset_requested.emit()
            self.accept()


class EraseConfirmDialog(QDialog):
    """The last gate before a master reset: the user types the word.

    A yes/no box is muscle memory. Typing a word is not, and this is the one
    action in Studio that cannot be undone by re-scanning — labels, people and
    corrections are gone with it, so the confirmation is deliberately slower
    than the mistake it prevents.
    """

    #: What has to be typed, exactly, before the button becomes usable.
    PHRASE = "ERASE"

    def __init__(
        self,
        summary: str,
        tokens: StudioTokens = TOKENS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setWindowTitle("Erase everything?")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_heading("Erase everything?", self))
        detail = QLabel(summary, self)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color:{tokens.ink}; font-size:12px;")
        layout.addWidget(detail)
        layout.addWidget(
            _copy(
                "Your original files stay exactly where they are. Everything Studio worked out "
                "about them does not.",
                tokens,
                self,
            )
        )
        layout.addWidget(_section(f"Type {self.PHRASE} to confirm", self))
        self.entry = QLineEdit(self)
        self.entry.setPlaceholderText(self.PHRASE)
        self.entry.textChanged.connect(self._validate)
        layout.addWidget(self.entry)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = AnimatedButton("Keep my library", variant="secondary", tokens=tokens, parent=self)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.confirm = AnimatedButton(
            "Erase everything", variant="danger", tokens=tokens, parent=self
        )
        self.confirm.setEnabled(False)
        self.confirm.clicked.connect(self.accept)
        actions.addWidget(self.confirm)
        layout.addLayout(actions)

    def _validate(self, text: str) -> None:
        self.confirm.setEnabled(text.strip().upper() == self.PHRASE)


class FolderManagerDialog(QDialog):
    add_requested = Signal()
    remove_requested = Signal(object)
    scan_requested = Signal(object)

    def __init__(
        self, roots: list[Path], tokens: StudioTokens = TOKENS, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setWindowTitle("Library Folders")
        self.resize(840, 580)
        self.setMinimumSize(700, 480)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(_heading("Library folders", self))
        titles.addWidget(
            _copy(
                "Studio watches these locations. Removing one deletes only its index and cache "
                "entries — never your files.",
                tokens,
                self,
            )
        )
        header.addLayout(titles, 1)
        add = AnimatedButton("Add folder", variant="primary", icon_text="+", tokens=tokens, parent=self)
        add.clicked.connect(self.add_requested)
        header.addWidget(add)
        outer.addLayout(header)
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.host = QWidget(self.area)
        self.rows = QVBoxLayout(self.host)
        self.rows.setContentsMargins(2, 2, 8, 2)
        self.rows.setSpacing(10)
        self.area.setWidget(self.host)
        outer.addWidget(self.area, 1)
        close = AnimatedButton("Done", variant="secondary", tokens=tokens, parent=self)
        close.clicked.connect(self.accept)
        outer.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.set_roots(roots)

    def set_roots(self, roots: list[Path]) -> None:
        clear_layout(self.rows)
        if not roots:
            empty = Panel(self.tokens, self.host)
            title = QLabel("No folders yet", empty)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet("font-size:18px; font-weight:650;")
            empty.body.addWidget(title)
            empty.body.addWidget(_copy("Add a folder or drag one onto the main window.", self.tokens, empty))
            self.rows.addWidget(empty)
        for root in roots:
            self.rows.addWidget(self._root_row(root))
        self.rows.addStretch(1)

    def _root_row(self, root: Path) -> QWidget:
        row = Panel(self.tokens, self.host)
        row.body.setContentsMargins(16, 12, 12, 12)
        line = QHBoxLayout()
        glyph = QLabel("▰", row)
        glyph.setStyleSheet(f"color:{self.tokens.brand}; font-size:20px;")
        line.addWidget(glyph)
        detail = QVBoxLayout()
        detail.setSpacing(2)
        name = QLabel(root.name or str(root), row)
        name.setStyleSheet("font-weight:650;")
        path = QLabel(str(root), row)
        path.setStyleSheet(f"color:{self.tokens.ink_soft}; font-size:11px;")
        online = root.is_dir()
        status = QLabel("Watching" if online else "Folder is offline", row)
        status.setStyleSheet(
            f"color:{self.tokens.mint if online else self.tokens.danger}; font-size:10px; font-weight:600;"
        )
        detail.addWidget(name)
        detail.addWidget(path)
        detail.addWidget(status)
        line.addLayout(detail, 1)
        browse = AnimatedButton("Open", variant="ghost", tokens=self.tokens, parent=row)
        browse.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(root))))
        scan = AnimatedButton("Scan", variant="secondary", tokens=self.tokens, parent=row)
        scan.clicked.connect(lambda: self.scan_requested.emit(root))
        remove = AnimatedButton("Remove", variant="danger", tokens=self.tokens, parent=row)
        remove.clicked.connect(lambda: self.remove_requested.emit(root))
        for button in (browse, scan, remove):
            line.addWidget(button)
        row.body.addLayout(line)
        return row


class AnalyzerCard(Panel):
    """One analyzer, with its queue state and a scoped run control."""

    toggle_requested = Signal(dict)
    run_requested = Signal(dict)
    configure_requested = Signal(dict)

    def __init__(
        self, analyzer: dict[str, object], tokens: StudioTokens, parent: QWidget | None = None
    ) -> None:
        super().__init__(tokens, parent)
        self.analyzer = analyzer
        self.body.setContentsMargins(16, 14, 14, 14)
        self.body.setSpacing(9)
        top = QHBoxLayout()
        top.setSpacing(12)
        kind = str(analyzer.get("kind") or "Add-on")
        icon = QLabel("✦" if kind in {"Local LLM", "Remote API"} else "◈", self)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{tokens.brand_soft}; color:{tokens.brand}; border-radius:13px; font-size:18px;"
        )
        top.addWidget(icon)
        detail = QVBoxLayout()
        detail.setSpacing(3)
        name = QLabel(str(analyzer.get("plugin_id") or "Analyzer"), self)
        name.setStyleSheet("font-size:13px; font-weight:700;")
        description = QLabel(
            str(
                analyzer.get("load_error")
                or analyzer.get("description")
                or "Adds searchable metadata to your library."
            ),
            self,
        )
        description.setWordWrap(True)
        description.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        enabled = bool(analyzer.get("enabled"))
        state = "Enabled" if enabled else "Available"
        meta = QLabel(f"{kind}  ·  {state}  ·  v{analyzer.get('version') or '1'}", self)
        meta.setStyleSheet(
            f"color:{tokens.mint if enabled else tokens.ink_faint}; font-size:10px; font-weight:600;"
        )
        detail.addWidget(name)
        detail.addWidget(description)
        detail.addWidget(meta)
        top.addLayout(detail, 1)
        if analyzer.get("configurable"):
            configure = AnimatedButton("Configure", variant="ghost", tokens=tokens, parent=self)
            configure.clicked.connect(lambda: self.configure_requested.emit(self.analyzer))
            top.addWidget(configure)
        toggle = AnimatedButton(
            "Disable" if enabled else "Enable", variant="secondary", tokens=tokens, parent=self
        )
        toggle.clicked.connect(lambda: self.toggle_requested.emit(self.analyzer))
        top.addWidget(toggle)
        self.body.addLayout(top)

        tasks = analyzer.get("tasks")
        counts = tasks if isinstance(tasks, dict) else {}
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        queue = QLabel(self._queue_text(counts), self)
        queue.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        run_row.addWidget(queue, 1)
        self.scope = QComboBox(self)
        self.scope.addItems([label for label, _ in RUN_SCOPES])
        raw_accepts = analyzer.get("accepts")
        accepts = {str(value) for value in raw_accepts} if isinstance(raw_accepts, list) else set()
        self.scope.setCurrentText("Videos only" if "video" in accepts else "Everything")
        self.scope.setEnabled(enabled)
        run_row.addWidget(self.scope)
        failed = int(counts.get("failed", 0) or 0)
        if failed:
            retry = AnimatedButton("Retry failed", variant="ghost", tokens=tokens, parent=self)
            retry.clicked.connect(lambda: self._emit_run(retry_failed=True))
            run_row.addWidget(retry)
        run = AnimatedButton("Run", variant="primary", icon_text="▶", tokens=tokens, parent=self)
        run.setEnabled(enabled and not bool(analyzer.get("load_error")))
        run.clicked.connect(lambda: self._emit_run(retry_failed=False))
        run_row.addWidget(run)
        self.body.addLayout(run_row)

    @staticmethod
    def _queue_text(counts: dict[str, Any]) -> str:
        if not counts:
            return "Nothing queued yet."
        parts = [
            f"{int(value):,} {state}"
            for state, value in sorted(counts.items())
            if int(value or 0)
        ]
        return "  ·  ".join(parts) or "Nothing queued yet."

    def _emit_run(self, *, retry_failed: bool) -> None:
        scopes = dict(RUN_SCOPES)
        payload = dict(self.analyzer)
        payload["media_types"] = list(scopes.get(self.scope.currentText(), ()))
        payload["scope_label"] = self.scope.currentText()
        payload["retry_failed"] = retry_failed
        self.run_requested.emit(payload)


class ModelCard(Panel):
    """One model: installed and usable, or available to download."""

    install_requested = Signal(str)
    use_requested = Signal(dict)
    cancel_requested = Signal()

    def __init__(
        self,
        model: dict[str, Any],
        tokens: StudioTokens,
        parent: QWidget | None = None,
        *,
        in_use: bool = False,
    ) -> None:
        super().__init__(tokens, parent)
        self.model = model
        self.key = str(model.get("key") or "")
        self.body.setContentsMargins(16, 14, 14, 14)
        self.body.setSpacing(9)
        installed = bool(model.get("installed"))
        vision = bool(model.get("vision"))

        top = QHBoxLayout()
        top.setSpacing(12)
        icon = QLabel("👁" if vision else "≡", self)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{tokens.brand_soft if vision else tokens.surface_soft};"
            f"color:{tokens.brand if vision else tokens.ink_soft};"
            "border-radius:13px; font-size:18px;"
        )
        top.addWidget(icon)

        detail = QVBoxLayout()
        detail.setSpacing(3)
        title = QLabel(str(model.get("display_name") or self.key), self)
        title.setStyleSheet("font-size:13px; font-weight:700;")
        detail.addWidget(title)
        summary = QLabel(str(model.get("summary") or ""), self)
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        detail.addWidget(summary)
        badges: list[str] = []
        if in_use:
            badges.append("USED FOR TAGGING")
        elif installed:
            badges.append("INSTALLED")
        else:
            badges.append(f"DOWNLOAD ≈ {human_size(model.get('size_bytes'))}")
        if model.get("loaded"):
            badges.append("IN MEMORY")
        badges.append("SEES VIDEO" if vision else "TEXT ONLY")
        if model.get("vram_gb"):
            badges.append(f"~{model['vram_gb']:.0f} GB VRAM")
        meta = QLabel("  ·  ".join(badges), self)
        meta.setStyleSheet(
            f"color:{tokens.mint if in_use else tokens.ink_faint}; font-size:10px; font-weight:700;"
        )
        detail.addWidget(meta)
        top.addLayout(detail, 1)

        if installed:
            use = AnimatedButton(
                "In use" if in_use else "Use for tagging",
                variant="primary" if not in_use else "secondary",
                tokens=tokens,
                parent=self,
            )
            use.setEnabled(not in_use)
            use.clicked.connect(lambda: self.use_requested.emit(self.model))
            top.addWidget(use)
        else:
            self.install = AnimatedButton(
                "Install", variant="primary", icon_text="↓", tokens=tokens, parent=self
            )
            self.install.clicked.connect(lambda: self.install_requested.emit(self.key))
            top.addWidget(self.install)
        self.body.addLayout(top)

        self.progress = QProgressBar(self)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.body.addWidget(self.progress)
        self.status = QLabel("", self)
        self.status.setStyleSheet(f"color:{tokens.ink_soft}; font-size:10px;")
        self.status.hide()
        self.body.addWidget(self.status)

    def show_progress(self, fraction: float | None, message: str) -> None:
        self.progress.show()
        self.status.show()
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(fraction * 1000))
        self.status.setText(message[:120])


class FilterPackCard(Panel):
    """One installable taxonomy: what it sorts into, and whether it is on."""

    toggle_requested = Signal(dict)
    run_requested = Signal(dict)
    uninstall_requested = Signal(dict)

    def __init__(
        self, pack: dict[str, Any], tokens: StudioTokens, parent: QWidget | None = None
    ) -> None:
        super().__init__(tokens, parent)
        self.pack = pack
        self.body.setContentsMargins(16, 14, 14, 14)
        self.body.setSpacing(9)
        enabled = bool(pack.get("enabled"))
        method = str(pack.get("method") or "vision")

        top = QHBoxLayout()
        top.setSpacing(12)
        glyph = {"rules": "⌘", "vision": "👁", "text": "≡"}.get(method, "◈")
        icon = QLabel(glyph, self)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{tokens.brand_soft}; color:{tokens.brand};"
            "border-radius:13px; font-size:18px;"
        )
        top.addWidget(icon)

        detail = QVBoxLayout()
        detail.setSpacing(3)
        title = QLabel(str(pack.get("name") or pack.get("pack_id")), self)
        title.setStyleSheet("font-size:13px; font-weight:700;")
        detail.addWidget(title)
        summary = QLabel(str(pack.get("description") or ""), self)
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        detail.addWidget(summary)
        labels = [str(item.get("display") or item.get("name")) for item in pack.get("labels", [])]
        preview = QLabel("  ·  ".join(labels[:7]) + ("  ·  …" if len(labels) > 7 else ""), self)
        preview.setWordWrap(True)
        preview.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        detail.addWidget(preview)
        badges = [
            "FILTERS BY " + str(pack.get("facet") or "").upper(),
            f"{len(labels)} VALUES",
            "NEEDS A VISION MODEL" if method == "vision" else
            "NEEDS A LOCAL MODEL" if method == "text" else "NO MODEL NEEDED",
        ]
        if not pack.get("builtin"):
            badges.append("CUSTOM")
        meta = QLabel("  ·  ".join(badges), self)
        meta.setStyleSheet(
            f"color:{tokens.mint if enabled else tokens.ink_faint}; font-size:9px; font-weight:700;"
        )
        detail.addWidget(meta)
        top.addLayout(detail, 1)

        toggle = AnimatedButton(
            "Remove" if enabled else "Add filter",
            variant="secondary" if enabled else "primary",
            icon_text="" if enabled else "+",
            tokens=tokens,
            parent=self,
        )
        toggle.clicked.connect(lambda: self.toggle_requested.emit(self.pack))
        top.addWidget(toggle)
        if not pack.get("builtin"):
            # Only packs the user installed can be deleted. A shipped one is
            # part of the application; turning it off is what "removing" it
            # means, and offering Delete for it would promise otherwise.
            uninstall = AnimatedButton("Delete", variant="ghost", tokens=tokens, parent=self)
            uninstall.setToolTip("Delete this filter file. Tags it already made are kept.")
            uninstall.clicked.connect(lambda: self.uninstall_requested.emit(self.pack))
            top.addWidget(uninstall)
        self.body.addLayout(top)

        tasks = pack.get("tasks")
        counts = tasks if isinstance(tasks, dict) else {}
        row = QHBoxLayout()
        row.setSpacing(8)
        state = QLabel(AnalyzerCard._queue_text(counts) if enabled else "Not added yet.", self)
        state.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        row.addWidget(state, 1)
        if enabled:
            self.scope = QComboBox(self)
            self.scope.addItems([label for label, _ in RUN_SCOPES])
            accepts = {str(value) for value in (pack.get("accepts") or [])}
            self.scope.setCurrentText("Videos only" if "video" in accepts else "Everything")
            row.addWidget(self.scope)
            run = AnimatedButton("Sort now", variant="primary", icon_text="▶", tokens=tokens, parent=self)
            run.clicked.connect(self._emit_run)
            row.addWidget(run)
        self.body.addLayout(row)

    def _emit_run(self) -> None:
        scopes = dict(RUN_SCOPES)
        payload = dict(self.pack)
        payload["media_types"] = list(scopes.get(self.scope.currentText(), ()))
        payload["scope_label"] = self.scope.currentText()
        payload["retry_failed"] = False
        self.run_requested.emit(payload)


class VisionModelCard(Panel):
    """One open-source model: what it tags, what it costs, where to get it."""

    open_requested = Signal(str)
    use_requested = Signal()

    def __init__(
        self, model: dict[str, Any], tokens: StudioTokens, parent: QWidget | None = None
    ) -> None:
        super().__init__(tokens, parent)
        self.model = model
        self.body.setContentsMargins(16, 14, 14, 14)
        self.body.setSpacing(9)
        local = bool(model.get("local"))

        top = QHBoxLayout()
        top.setSpacing(12)
        glyph = {
            "tagging": "◈", "nsfw": "◐", "detection": "▣", "faces": "☺",
            "scenes": "▤", "speech": "▶", "audio": "♪", "text": "≡", "embedding": "✦",
        }.get(str(model.get("task")), "◈")
        icon = QLabel(glyph, self)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{tokens.brand_soft}; color:{tokens.brand};"
            "border-radius:13px; font-size:18px;"
        )
        top.addWidget(icon)

        detail = QVBoxLayout()
        detail.setSpacing(3)
        title = QLabel(str(model.get("name") or model.get("id")), self)
        title.setStyleSheet("font-size:13px; font-weight:700;")
        detail.addWidget(title)
        summary = QLabel(str(model.get("summary") or ""), self)
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        detail.addWidget(summary)

        size = int(model.get("size_mb") or 0)
        badges = [
            str(model.get("task", "")).upper(),
            "RUNS HERE" if local else f"NEEDS {str(model.get('runtime', '')).upper()}",
            f"TAGS → {str(model.get('namespace', '')).upper()}",
        ]
        if model.get("timestamps"):
            badges.append("PER-FRAME TIMESTAMPS")
        if size:
            badges.append(f"~{size} MB")
        badges.append(str(model.get("license", "")))
        meta = QLabel("  ·  ".join(badge for badge in badges if badge), self)
        meta.setWordWrap(True)
        meta.setStyleSheet(
            f"color:{tokens.mint if local else tokens.ink_faint}; font-size:9px; font-weight:700;"
        )
        detail.addWidget(meta)
        top.addLayout(detail, 1)

        page = AnimatedButton(
            "Open on GitHub", variant="secondary", icon_text="↗", tokens=tokens, parent=self
        )
        page.clicked.connect(lambda: self.open_requested.emit(str(self.model.get("url") or "")))
        top.addWidget(page)
        if local and str(model.get("runtime")) == "onnx":
            use = AnimatedButton("Use file…", variant="primary", tokens=tokens, parent=self)
            use.setToolTip("Point the tagger at this model once you have downloaded it")
            use.clicked.connect(self.use_requested)
            top.addWidget(use)
        self.body.addLayout(top)

        note = str(model.get("notes") or "")
        if note:
            hint = QLabel(note, self)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
            self.body.addWidget(hint)


class AnalyzerStoreDialog(QDialog):
    """Filters, analyzers, downloadable models, and queue health in one window."""

    toggle_requested = Signal(dict)
    run_requested = Signal(dict)
    configure_requested = Signal(dict)
    add_api_requested = Signal()
    lm_studio_requested = Signal()
    install_model_requested = Signal(str)
    use_model_requested = Signal(dict)
    cancel_install_requested = Signal()
    refresh_requested = Signal()
    open_pack_folder_requested = Signal()
    install_pack_requested = Signal()
    choose_vision_model_requested = Signal()
    open_model_page_requested = Signal(str)
    uninstall_pack_requested = Signal(dict)

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.catalog: list[dict[str, object]] = []
        self.model_state: dict[str, Any] = {}
        self.pack_state: dict[str, Any] = {}
        self.installing: str = ""
        self._cards: dict[str, ModelCard] = {}
        self.setWindowTitle("AI Analyzer Store")
        self.resize(1000, 720)
        self.setMinimumSize(820, 600)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(13)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(_heading("AI Analyzer Store", self))
        titles.addWidget(
            _copy(
                "Add local intelligence for filtering and tagging. Originals stay read-only and "
                "network access is always explicit.",
                tokens,
                self,
            )
        )
        header.addLayout(titles, 1)
        lm = AnimatedButton("LM Studio settings", variant="secondary", tokens=tokens, parent=self)
        lm.clicked.connect(self.lm_studio_requested)
        api = AnimatedButton("Add analyzer API", variant="ghost", icon_text="+", tokens=tokens, parent=self)
        api.clicked.connect(self.add_api_requested)
        header.addWidget(api)
        header.addWidget(lm)
        outer.addLayout(header)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._packs_tab(), "Filters")
        self.tabs.addTab(self._vision_tab(), "Tagging models")
        self.tabs.addTab(self._models_tab(), "Chat models")
        self.tabs.addTab(self._analyzers_tab(), "Analyzers")
        outer.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        refresh = AnimatedButton("Refresh", variant="ghost", icon_text="↻", tokens=tokens, parent=self)
        refresh.clicked.connect(self.refresh_requested)
        footer.addWidget(refresh)
        footer.addStretch(1)
        done = AnimatedButton("Done", variant="secondary", tokens=tokens, parent=self)
        done.clicked.connect(self.accept)
        footer.addWidget(done)
        outer.addLayout(footer)

    # ── filters tab ─────────────────────────────────────────────────────────

    def _packs_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(11)
        self.pack_banner = QLabel(
            "Each filter sorts your library along one axis — what sport it is, whether it is "
            "animated, where it came from. Add the ones you want; every value becomes a "
            "search filter and a facet.",
            tab,
        )
        self.pack_banner.setWordWrap(True)
        self.pack_banner.setStyleSheet(
            f"background:{self.tokens.brand_soft}; color:{self.tokens.brand};"
            f"border-radius:{self.tokens.radius_md}px; padding:12px 15px;"
            "font-size:11px; font-weight:600;"
        )
        layout.addWidget(self.pack_banner)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.pack_search = QLineEdit(tab)
        self.pack_search.setPlaceholderText("Search filters — genre, sport, cartoon, twitch, adult…")
        self.pack_search.setClearButtonEnabled(True)
        self.pack_search.textChanged.connect(self._render_packs)
        controls.addWidget(self.pack_search, 1)
        install = AnimatedButton(
            "Install from file…", variant="secondary", icon_text="↓", tokens=self.tokens, parent=tab
        )
        install.setToolTip("Add a filter pack someone else wrote, from a .toml file")
        install.clicked.connect(self.install_pack_requested)
        controls.addWidget(install)
        folder = AnimatedButton(
            "Write your own", variant="ghost", icon_text="✎", tokens=self.tokens, parent=tab
        )
        folder.setToolTip("Open the folder where custom filter packs live")
        folder.clicked.connect(self.open_pack_folder_requested)
        controls.addWidget(folder)
        layout.addLayout(controls)

        self.pack_area = QScrollArea(tab)
        self.pack_area.setWidgetResizable(True)
        self.pack_host = QWidget(self.pack_area)
        self.pack_rows = QVBoxLayout(self.pack_host)
        self.pack_rows.setContentsMargins(2, 2, 8, 2)
        self.pack_rows.setSpacing(9)
        self.pack_area.setWidget(self.pack_host)
        layout.addWidget(self.pack_area, 1)
        self._placeholder(self.pack_rows, self.pack_host, "Loading filters…")
        return tab

    def set_pack_state(self, state: dict[str, Any]) -> None:
        self.pack_state = state
        self._render_packs()

    def _render_packs(self) -> None:
        clear_layout(self.pack_rows)
        state = getattr(self, "pack_state", None)
        if not state:
            self._placeholder(self.pack_rows, self.pack_host, "Loading filters…")
            return
        errors = state.get("errors") or {}
        if errors:
            detail = "; ".join(f"{Path(path).name}: {reason}" for path, reason in errors.items())
            self.pack_banner.setText(f"⚠  Some filter files could not be read — {detail}")
        query = self.pack_search.text().strip().lower()
        packs = [row for row in state.get("packs", []) if isinstance(row, dict)]
        if query:
            packs = [
                row
                for row in packs
                if query in " ".join(str(value) for value in row.values()).lower()
            ]
        if not packs:
            self._placeholder(
                self.pack_rows,
                self.pack_host,
                "No filters match that search."
                if state.get("packs")
                else "No filter packs found.",
            )
            return
        for pack in packs:
            card = FilterPackCard(pack, self.tokens, self.pack_host)
            card.toggle_requested.connect(self.toggle_requested)
            card.run_requested.connect(self.run_requested)
            card.uninstall_requested.connect(self.uninstall_pack_requested)
            self.pack_rows.addWidget(card)
        self.pack_rows.addStretch(1)

    # ── tagging models tab ──────────────────────────────────────────────────

    def _vision_tab(self) -> QWidget:
        """Open-source vision models: what they do, and where they live.

        Separate from the chat-model store on purpose. Tagging a library and
        answering questions about it are different jobs with different right
        answers, and the tab that says "pick a model" should not offer a 30 GB
        language model for a job a 380 MB tagger does better and 100x faster.
        """
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(11)

        self.vision_banner = QLabel("", tab)
        self.vision_banner.setWordWrap(True)
        self.vision_banner.setStyleSheet(
            f"background:{self.tokens.brand_soft}; color:{self.tokens.brand};"
            f"border-radius:{self.tokens.radius_md}px; padding:12px 15px;"
            "font-size:11px; font-weight:600;"
        )
        layout.addWidget(self.vision_banner)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.vision_search = QLineEdit(tab)
        self.vision_search.setPlaceholderText(
            "Search tagging models — tags, nsfw, faces, objects, speech, timestamps…"
        )
        self.vision_search.setClearButtonEnabled(True)
        self.vision_search.textChanged.connect(self._render_vision)
        controls.addWidget(self.vision_search, 1)
        self.vision_task = QComboBox(tab)
        self.vision_task.addItem("Every kind")
        self.vision_task.currentTextChanged.connect(self._render_vision)
        controls.addWidget(self.vision_task)
        choose = AnimatedButton(
            "Use a model file…", variant="primary", icon_text="◎", tokens=self.tokens, parent=tab
        )
        choose.setToolTip("Point the local tagger at an .onnx file you downloaded")
        choose.clicked.connect(self.choose_vision_model_requested)
        controls.addWidget(choose)
        layout.addLayout(controls)

        self.vision_area = QScrollArea(tab)
        self.vision_area.setWidgetResizable(True)
        self.vision_host = QWidget(self.vision_area)
        self.vision_rows = QVBoxLayout(self.vision_host)
        self.vision_rows.setContentsMargins(2, 2, 8, 2)
        self.vision_rows.setSpacing(9)
        self.vision_area.setWidget(self.vision_host)
        layout.addWidget(self.vision_area, 1)
        self._placeholder(self.vision_rows, self.vision_host, "Loading models…")
        return tab

    def set_vision_state(self, state: dict[str, Any]) -> None:
        """Catalogue rows, runtime availability, and the model in use."""
        self.vision_state = state
        tasks = [str(item) for item in (state.get("tasks") or [])]
        if tasks and self.vision_task.count() <= 1:
            self.vision_task.addItems([name.title() for name in tasks])
        self._render_vision()

    def _render_vision(self) -> None:
        clear_layout(self.vision_rows)
        state = getattr(self, "vision_state", None)
        if not state:
            self._placeholder(self.vision_rows, self.vision_host, "Loading models…")
            return

        runtimes = state.get("runtimes") or {}
        execution = state.get("execution") or {}
        active = str(state.get("model_path") or "")
        # Naming the device is the difference between a library tagged
        # overnight and one tagged over a fortnight, and nothing else in the
        # interface would reveal a silent fall back to CPU.
        device = "your GPU" if execution.get("gpu") else "the CPU"
        if not runtimes.get("onnxruntime"):
            self.vision_banner.setText(
                "⚠  No ONNX runtime is installed, so no local tagging model can run yet. "
                "Install it with:   pip install onnxruntime-directml   (Windows GPU) "
                "or   pip install onnxruntime   (CPU)"
            )
        elif active:
            self.vision_banner.setText(
                f"Tagging with {Path(active).name} on {device} — every video tag is recorded "
                "with the second it was seen. No language model involved."
            )
        else:
            self.vision_banner.setText(
                f"Local models tag without a language model: faster, offline, and they cannot "
                f"invent a label. Ready to run on {device}. Download one below, then choose "
                "“Use a model file…”. Videos are tagged frame by frame, so every tag carries "
                "a timestamp."
            )

        query = self.vision_search.text().strip().lower()
        chosen_task = self.vision_task.currentText()
        rows = [row for row in (state.get("models") or []) if isinstance(row, dict)]
        if chosen_task and chosen_task != "Every kind":
            rows = [row for row in rows if str(row.get("task", "")).lower() == chosen_task.lower()]
        if query:
            rows = [
                row
                for row in rows
                if query in " ".join(str(value) for value in row.values()).lower()
            ]
        if not rows:
            self._placeholder(self.vision_rows, self.vision_host, "No models match that search.")
            return
        for row in rows:
            card = VisionModelCard(row, self.tokens, self.vision_host)
            card.open_requested.connect(self.open_model_page_requested)
            card.use_requested.connect(self.choose_vision_model_requested)
            self.vision_rows.addWidget(card)
        self.vision_rows.addStretch(1)

    # ── chat models tab ─────────────────────────────────────────────────────

    def _models_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(11)

        self.model_banner = QLabel("Checking LM Studio…", tab)
        self.model_banner.setWordWrap(True)
        self.model_banner.setStyleSheet(
            f"background:{self.tokens.brand_soft}; color:{self.tokens.brand};"
            f"border-radius:{self.tokens.radius_md}px; padding:12px 15px; font-size:11px; font-weight:600;"
        )
        layout.addWidget(self.model_banner)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.model_search = QLineEdit(tab)
        self.model_search.setPlaceholderText("Search models, or paste a name / Hugging Face URL to install…")
        self.model_search.setClearButtonEnabled(True)
        self.model_search.textChanged.connect(self._render_models)
        self.model_search.returnPressed.connect(self._install_typed)
        controls.addWidget(self.model_search, 1)
        self.vision_only = CheckBox("Only models that can see video", self.tokens, tab)
        self.vision_only.setChecked(True)
        self.vision_only.toggled.connect(self._render_models)
        controls.addWidget(self.vision_only)
        install_typed = AnimatedButton(
            "Install typed name", variant="secondary", icon_text="↓", tokens=self.tokens, parent=tab
        )
        install_typed.clicked.connect(self._install_typed)
        controls.addWidget(install_typed)
        layout.addLayout(controls)

        self.model_area = QScrollArea(tab)
        self.model_area.setWidgetResizable(True)
        self.model_host = QWidget(self.model_area)
        self.model_rows = QVBoxLayout(self.model_host)
        self.model_rows.setContentsMargins(2, 2, 8, 2)
        self.model_rows.setSpacing(9)
        self.model_area.setWidget(self.model_host)
        layout.addWidget(self.model_area, 1)
        self._placeholder(self.model_rows, self.model_host, "Loading model list…")
        return tab

    def _install_typed(self) -> None:
        typed = self.model_search.text().strip()
        if typed:
            self.install_model_requested.emit(typed)

    def _placeholder(self, layout: QVBoxLayout, host: QWidget, text: str) -> None:
        label = QLabel(text, host)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(f"color:{self.tokens.ink_soft}; padding:40px;")
        layout.addWidget(label)

    def set_model_state(self, state: dict[str, Any]) -> None:
        self.model_state = state
        self._render_models()

    def _render_models(self) -> None:
        clear_layout(self.model_rows)
        self._cards.clear()
        state = self.model_state
        if not state:
            self._placeholder(self.model_rows, self.model_host, "Loading model list…")
            return
        configured = str(state.get("configured") or "")
        served = list(state.get("served") or [])
        if not state.get("cli_available"):
            self.model_banner.setText(
                "LM Studio's command line tool was not found, so Studio cannot install models for "
                "you. Install LM Studio and run 'lms bootstrap' once — models you install by hand "
                "still appear here."
            )
        elif configured and served and configured not in served:
            self.model_banner.setText(
                f"The analyzer is set to use “{configured}”, which LM Studio is not currently "
                "serving. Pick a model below and choose “Use for tagging”."
            )
        elif configured:
            self.model_banner.setText(f"Tagging with “{configured}”. Install another model to switch.")
        else:
            self.model_banner.setText(
                "No model chosen yet. Pick one that can see video, then run the LM Studio analyzer "
                "to categorize your clips."
            )

        query = self.model_search.text().strip().lower()
        rows = [row for row in state.get("models", []) if isinstance(row, dict)]
        if self.vision_only.isChecked():
            rows = [row for row in rows if row.get("vision")]
        if query:
            rows = [
                row
                for row in rows
                if query in " ".join(str(value) for value in row.values()).lower()
            ]
        if not rows:
            self._placeholder(
                self.model_rows,
                self.model_host,
                "No models match that search.\nPress Enter to install it by name from LM Studio.",
            )
            return
        for row in rows:
            card = ModelCard(row, self.tokens, self.model_host, in_use=str(row.get("key")) == configured)
            card.install_requested.connect(self.install_model_requested)
            card.use_requested.connect(self.use_model_requested)
            self.model_rows.addWidget(card)
            self._cards[str(row.get("key") or "")] = card
        self.model_rows.addStretch(1)

    def show_install_progress(self, key: str, fraction: float | None, message: str) -> None:
        """Route download progress to the card being installed."""
        self.installing = key
        card = self._cards.get(key)
        if card is not None:
            card.show_progress(fraction, message)

    # ── analyzers tab ───────────────────────────────────────────────────────

    def _analyzers_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(11)
        self.search = QLineEdit(tab)
        self.search.setPlaceholderText("Search vision, metadata, local AI, tags, APIs…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render_analyzers)
        layout.addWidget(self.search)
        self.analyzer_area = QScrollArea(tab)
        self.analyzer_area.setWidgetResizable(True)
        self.analyzer_host = QWidget(self.analyzer_area)
        self.rows = QVBoxLayout(self.analyzer_host)
        self.rows.setContentsMargins(2, 2, 8, 2)
        self.rows.setSpacing(9)
        self.analyzer_area.setWidget(self.analyzer_host)
        layout.addWidget(self.analyzer_area, 1)
        self._placeholder(self.rows, self.analyzer_host, "Loading analyzer catalog…")
        return tab

    def set_catalog(self, catalog: list[dict[str, object]]) -> None:
        self.catalog = catalog
        self._render_analyzers()

    def _render_analyzers(self) -> None:
        clear_layout(self.rows)
        query = self.search.text().strip().lower()
        matches = [
            row
            for row in self.catalog
            if not query or query in " ".join(str(value) for value in row.values()).lower()
        ]
        if not matches:
            self._placeholder(
                self.rows,
                self.analyzer_host,
                "No analyzers match that search." if self.catalog else "Loading analyzer catalog…",
            )
            return
        for analyzer in matches:
            card = AnalyzerCard(analyzer, self.tokens, self.analyzer_host)
            card.toggle_requested.connect(self.toggle_requested)
            card.run_requested.connect(self.run_requested)
            card.configure_requested.connect(self.configure_requested)
            self.rows.addWidget(card)
        self.rows.addStretch(1)
