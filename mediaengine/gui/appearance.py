"""Appearance primitives for the V1 desktop surface.

This module deliberately contains no MediaEngine imports.  Themes, persisted
UI preferences, gradients, and placeholder behavior can evolve without
coupling visual decisions to indexing or database code.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import Canvas, StringVar, ttk
from typing import Any, cast

from ..config import Config


@dataclass(frozen=True, slots=True)
class Theme:
    """A complete semantic color palette, independent of widget type."""

    name: str
    bg: str
    panel: str
    panel_alt: str
    raised: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    secondary: str
    border: str
    selection: str
    danger: str
    header_start: str
    header_end: str
    header_text: str
    header_muted: str
    is_dark: bool = False


THEMES: dict[str, Theme] = {
    "Claude Light": Theme(
        name="Claude Light",
        bg="#F7F5F0",
        panel="#FFFFFF",
        panel_alt="#F0ECE5",
        raised="#FAF8F4",
        text="#2D2A27",
        muted="#756E67",
        accent="#AD573F",
        accent_hover="#914530",
        secondary="#678176",
        border="#DED8CE",
        selection="#F4D8C9",
        danger="#B84A54",
        header_start="#EFE7DA",
        header_end="#F6DDD0",
        header_text="#292521",
        header_muted="#6E6259",
    ),
    "Midnight Ink": Theme(
        name="Midnight Ink",
        bg="#0E1420",
        panel="#171F2D",
        panel_alt="#202A3B",
        raised="#242F42",
        text="#F3EEE7",
        muted="#A7ADBA",
        accent="#E48A68",
        accent_hover="#F09B79",
        secondary="#6CB6A8",
        border="#303D53",
        selection="#35465E",
        danger="#F07A87",
        header_start="#1E2839",
        header_end="#3B2D35",
        header_text="#FFF8F1",
        header_muted="#C9BDB5",
        is_dark=True,
    ),
    "Sage Studio": Theme(
        name="Sage Studio",
        bg="#F2F4EF",
        panel="#FCFDF9",
        panel_alt="#E7ECE4",
        raised="#F8FAF5",
        text="#26312B",
        muted="#637168",
        accent="#A85F4B",
        accent_hover="#8E4C3C",
        secondary="#5E7E6D",
        border="#D3DCD2",
        selection="#DCE7DC",
        danger="#B44750",
        header_start="#DDE7DB",
        header_end="#F0DED1",
        header_text="#243029",
        header_muted="#5E6C64",
    ),
    "High Contrast": Theme(
        name="High Contrast",
        bg="#0A0A0A",
        panel="#151515",
        panel_alt="#242424",
        raised="#303030",
        text="#FFFFFF",
        muted="#D2D2D2",
        accent="#FF936F",
        accent_hover="#FFB096",
        secondary="#7FE4D4",
        border="#707070",
        selection="#49576B",
        danger="#FF8790",
        header_start="#171717",
        header_end="#3B2822",
        header_text="#FFFFFF",
        header_muted="#E2E2E2",
        is_dark=True,
    ),
}


@dataclass(slots=True)
class UISettings:
    """User-owned visual preferences stored separately from engine config."""

    theme: str = "Claude Light"
    text_scale: int = 100
    density: str = "Comfortable"
    reduced_motion: bool = False
    tutorial_completed: bool = False

    def normalize(self) -> "UISettings":
        if self.theme not in THEMES:
            self.theme = "Claude Light"
        self.text_scale = max(85, min(140, int(self.text_scale)))
        if self.density not in {"Compact", "Comfortable", "Cozy"}:
            self.density = "Comfortable"
        return self


def ui_settings_path(config: Config) -> Path:
    """Keep UI preferences beside the desktop config, never in the database."""
    if config.source_path is not None:
        return cast(Path, config.source_path.resolve().parent / "ui.json")
    return cast(Path, config.storage.db_path.resolve().parent / "ui.json")


def load_ui_settings(config: Config) -> UISettings:
    """Load appearance preferences; corrupt files fall back without blocking V1."""
    path = ui_settings_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return UISettings()
        allowed = {key: payload[key] for key in asdict(UISettings()) if key in payload}
        return UISettings(**allowed).normalize()
    except (OSError, ValueError, TypeError):
        return UISettings()


def save_ui_settings(config: Config, settings: UISettings) -> Path:
    """Atomically persist visual preferences."""
    destination = ui_settings_path(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(settings.normalize()), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def mix_color(left: str, right: str, fraction: float) -> str:
    """Interpolate two ``#RRGGBB`` colors."""
    fraction = max(0.0, min(1.0, fraction))
    a = tuple(int(left[index : index + 2], 16) for index in (1, 3, 5))
    b = tuple(int(right[index : index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(round(start + (end - start) * fraction) for start, end in zip(a, b))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def contrast_ratio(left: str, right: str) -> float:
    """Return the WCAG contrast ratio for two hexadecimal colors."""

    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(left), luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class GradientHeader(Canvas):
    """Responsive branded header with subtle, reduced-motion-aware animation."""

    def __init__(
        self,
        parent: Any,
        theme: Theme,
        *,
        scale: float = 1.0,
        reduced_motion: bool = False,
        on_help: Any = None,
        on_customize: Any = None,
    ) -> None:
        self.theme = theme
        self.ui_scale = scale
        self.reduced_motion = reduced_motion
        self.context = "Library"
        self.status = "Starting the library…"
        self.on_help = on_help
        self.on_customize = on_customize
        self._sweep = 1.0
        self._pulse = False
        super().__init__(
            parent,
            height=round(82 * scale),
            background=theme.header_start,
            highlightthickness=0,
            borderwidth=0,
        )
        self.bind("<Configure>", lambda _: self.redraw())
        self.bind("<Button-1>", self._clicked)
        self.after(900, self._tick)

    def apply_theme(self, theme: Theme, scale: float, reduced_motion: bool) -> None:
        self.theme = theme
        self.ui_scale = scale
        self.reduced_motion = reduced_motion
        self.configure(height=round(82 * scale), background=theme.header_start)
        self.redraw()

    def set_status(self, status: str) -> None:
        self.status = status
        self.redraw()

    def transition(self, context: str) -> None:
        self.context = context
        if self.reduced_motion:
            self._sweep = 1.0
            self.redraw()
            return
        self._sweep = 0.0
        self._animate_sweep()

    def _animate_sweep(self) -> None:
        self._sweep = min(1.0, self._sweep + 0.09)
        self.redraw()
        if self._sweep < 1.0:
            self.after(16, self._animate_sweep)

    def _tick(self) -> None:
        self._pulse = not self._pulse
        if not self.reduced_motion:
            self.redraw()
        self.after(900, self._tick)

    def _clicked(self, event: Any) -> None:
        tags = self.gettags("current")
        if "help" in tags and self.on_help is not None:
            self.on_help()
        elif "customize" in tags and self.on_customize is not None:
            self.on_customize()

    def redraw(self) -> None:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self.delete("all")
        bands = max(24, min(120, width // 10))
        for index in range(bands):
            start = index * width / bands
            end = (index + 1) * width / bands + 1
            self.create_rectangle(
                start,
                0,
                end,
                height,
                fill=mix_color(self.theme.header_start, self.theme.header_end, index / bands),
                outline="",
            )
        glow_width = width * 0.42 * self._sweep
        self.create_oval(
            width - glow_width,
            -height * 1.4,
            width + height,
            height * 1.8,
            fill=mix_color(self.theme.header_end, self.theme.accent, 0.12),
            outline="",
        )
        title_size = max(15, round(18 * self.ui_scale))
        small_size = max(8, round(9 * self.ui_scale))
        self.create_text(
            round(24 * self.ui_scale),
            round(27 * self.ui_scale),
            text="FILE INDEXER",
            anchor="w",
            fill=self.theme.header_text,
            font=("Segoe UI Semibold", title_size),
        )
        badge_x = round(166 * self.ui_scale)
        self.create_rectangle(
            badge_x,
            round(15 * self.ui_scale),
            badge_x + round(35 * self.ui_scale),
            round(38 * self.ui_scale),
            fill=self.theme.accent,
            outline="",
        )
        self.create_text(
            badge_x + round(17 * self.ui_scale),
            round(26 * self.ui_scale),
            text="V1",
            fill="#FFFFFF",
            font=("Segoe UI Semibold", small_size),
        )
        self.create_text(
            round(24 * self.ui_scale),
            round(56 * self.ui_scale),
            text=f"Special Edition  ·  {self.context}",
            anchor="w",
            fill=self.theme.header_muted,
            font=("Segoe UI", small_size),
        )
        if width > 760:
            dot = self.theme.secondary if not self._pulse else mix_color(self.theme.secondary, "#FFFFFF", 0.28)
            self.create_oval(width - round(318 * self.ui_scale), round(35 * self.ui_scale), width - round(308 * self.ui_scale), round(45 * self.ui_scale), fill=dot, outline="")
            self.create_text(width - round(298 * self.ui_scale), round(40 * self.ui_scale), text=self.status, anchor="w", fill=self.theme.header_muted, font=("Segoe UI", small_size))
        if self.on_help is not None:
            self.create_text(width - round(122 * self.ui_scale), round(40 * self.ui_scale), text="Help", tags=("help",), anchor="center", fill=self.theme.header_text, font=("Segoe UI Semibold", small_size))
        if self.on_customize is not None:
            self.create_text(width - round(50 * self.ui_scale), round(40 * self.ui_scale), text="Style", tags=("customize",), anchor="center", fill=self.theme.header_text, font=("Segoe UI Semibold", small_size))


class PlaceholderEntry(ttk.Entry):
    """A ttk entry with a real, non-querying placeholder state."""

    def __init__(self, parent: Any, variable: StringVar, placeholder: str, **kwargs: Any) -> None:
        self.value_variable = variable
        self.placeholder = placeholder
        self.showing_placeholder = False
        super().__init__(parent, textvariable=variable, **kwargs)
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self.value_variable.trace_add("write", self._variable_changed)
        self._show_placeholder()

    def value(self) -> str:
        return "" if self.showing_placeholder else self.value_variable.get().strip()

    def clear(self) -> None:
        self.showing_placeholder = False
        self.value_variable.set("")
        self.focus_set()

    def _show_placeholder(self) -> None:
        if not self.value_variable.get():
            self.showing_placeholder = True
            self.value_variable.set(self.placeholder)
            self.configure(style="Placeholder.TEntry")

    def _focus_in(self, _: Any) -> None:
        if self.showing_placeholder:
            self.showing_placeholder = False
            self.value_variable.set("")
            self.configure(style="TEntry")

    def _focus_out(self, _: Any) -> None:
        self._show_placeholder()

    def _variable_changed(self, *_: Any) -> None:
        current = self.value_variable.get()
        if self.showing_placeholder and current != self.placeholder:
            self.showing_placeholder = False
            self.configure(style="TEntry")
