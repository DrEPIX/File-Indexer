"""Design tokens for the Qt-based File Indexer Studio surface.

The token layer is intentionally independent from MediaEngine.  It keeps the
new UI internally consistent and gives future themes one small migration point
instead of scattering colors, radii, and motion timings across widgets.

Every palette carries three *glow* stops in addition to its flat colors.  They
exist because Studio paints gradients rather than flat fills: a flat token
alone cannot describe "violet in the top-left corner fading into peach at the
bottom-right".  Glow stops are deliberately kept within a narrow luminance band
of ``canvas`` so that text contrast is a property of the palette rather than of
where a widget happens to sit on the gradient.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass, replace

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QRadialGradient

__all__ = [
    "AUTO_DARK",
    "AUTO_LIGHT",
    "AUTO_PALETTE",
    "PALETTES",
    "TOKENS",
    "StudioTokens",
    "application_stylesheet",
    "contrast_ratio",
    "palette",
    "palette_choices",
    "palette_names",
    "system_is_dark",
]

#: The pseudo-palette meaning "whatever Windows is doing". Stored in settings
#: like any other choice, resolved at paint time rather than at pick time, so
#: the desktop switching to dark at sunset switches Studio too.
AUTO_PALETTE = "Match Windows"

#: What :data:`AUTO_PALETTE` resolves to in each system color scheme.
AUTO_LIGHT = "Aurora"
AUTO_DARK = "Midnight"


def _channels(value: str) -> tuple[float, float, float]:
    color = QColor(value)
    return color.redF(), color.greenF(), color.blueF()


def _luminance(value: str) -> float:
    """WCAG relative luminance, duplicated here to keep tokens dependency-free."""

    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in _channels(value)
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(left: str, right: str) -> float:
    """Return the WCAG contrast ratio between two colors."""

    high, low = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _hex(color: QColor) -> str:
    return f"#{color.red():02X}{color.green():02X}{color.blue():02X}"


def mix(left: str, right: str, amount: float) -> str:
    """Blend two colors in sRGB and return a ``#RRGGBB`` string."""

    amount = max(0.0, min(1.0, amount))
    start, end = QColor(left), QColor(right)
    return _hex(
        QColor(
            round(start.red() + (end.red() - start.red()) * amount),
            round(start.green() + (end.green() - start.green()) * amount),
            round(start.blue() + (end.blue() - start.blue()) * amount),
        )
    )


def _shift(value: str, *, hue: float = 0.0, saturation: float = 1.0, lightness: float = 1.0) -> str:
    """Rotate hue and scale saturation/lightness of a color in HLS space."""

    red, green, blue = _channels(value)
    h, l, s = colorsys.rgb_to_hls(red, green, blue)
    h = (h + hue / 360.0) % 1.0
    s = max(0.0, min(1.0, s * saturation))
    l = max(0.0, min(1.0, l * lightness))
    red, green, blue = colorsys.hls_to_rgb(h, l, s)
    return _hex(QColor.fromRgbF(red, green, blue))


def _lightness(value: str) -> float:
    red, green, blue = _channels(value)
    return colorsys.rgb_to_hls(red, green, blue)[1]


def _at_lightness(value: str, target: float) -> str:
    """Return ``value``'s hue and saturation at a specific HLS lightness."""

    red, green, blue = _channels(value)
    h, _, s = colorsys.rgb_to_hls(red, green, blue)
    red, green, blue = colorsys.hls_to_rgb(h, max(0.0, min(1.0, target)), s)
    return _hex(QColor.fromRgbF(red, green, blue))


@dataclass(frozen=True, slots=True)
class StudioTokens:
    """One complete visual contract: colors, gradients, geometry, and motion."""

    name: str = "Aurora"
    dark: bool = False
    canvas: str = "#F6F7FB"
    glow_a: str = "#EAE2FF"
    glow_b: str = "#FCEAE2"
    glow_c: str = "#E1F2EE"
    surface: str = "#FFFFFF"
    surface_soft: str = "#F0F1F7"
    surface_hover: str = "#EAE8F6"
    ink: str = "#1D1C24"
    ink_soft: str = "#5F5D6B"
    ink_faint: str = "#8B8996"
    brand: str = "#6B4EFF"
    brand_hover: str = "#5839EA"
    brand_soft: str = "#F4F2FF"
    accent: str = "#AE3CDE"
    on_brand: str = "#FFFFFF"
    coral: str = "#E16F55"
    mint: str = "#2C8073"
    danger: str = "#C4485F"
    border: str = "#E1E1E9"
    border_strong: str = "#CBC9D7"
    shadow: str = "#241D46"
    radius_sm: int = 10
    radius_md: int = 16
    radius_lg: int = 24
    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 24
    space_6: int = 32
    motion_fast: int = 130
    motion_normal: int = 190
    motion_slow: int = 280
    #: 0.0 renders every gradient as a flat fill; 1.0 is the designed intensity.
    intensity: float = 1.0

    # ── derived helpers ─────────────────────────────────────────────────────

    def color(self, value: str) -> QColor:
        return QColor(value)

    @property
    def glow_stops(self) -> tuple[str, str, str]:
        """The three canvas glow colors, in paint order."""
        return (self.glow_a, self.glow_b, self.glow_c)

    def toned(self, value: str, amount: float = 1.0) -> str:
        """Pull a color toward ``canvas`` by the inverse of gradient intensity.

        Gradient-heavy surfaces stay legible at low intensity because every
        decorative color collapses back onto the flat canvas rather than
        simply disappearing.
        """
        return mix(self.canvas, value, max(0.0, min(1.0, self.intensity * amount)))

    def veil(self, alpha: int) -> QColor:
        """A light or dark scrim appropriate to this palette's polarity."""
        base = QColor(255, 255, 255) if self.dark else QColor(0, 0, 0)
        base.setAlpha(max(0, min(255, alpha)))
        return base

    def shadow_color(self, alpha: int) -> QColor:
        color = QColor(self.shadow)
        color.setAlpha(max(0, min(255, round(alpha * (1.35 if self.dark else 1.0)))))
        return color

    # ── gradient factories ──────────────────────────────────────────────────

    def canvas_gradient(self, rect: QRectF) -> QLinearGradient:
        """The full-window wash: a diagonal drift across all three glow stops."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(self.toned(self.glow_a, 0.85)))
        gradient.setColorAt(0.45, QColor(self.canvas))
        gradient.setColorAt(0.78, QColor(self.toned(self.glow_b, 0.55)))
        gradient.setColorAt(1.0, QColor(self.toned(self.glow_c, 0.75)))
        return gradient

    def bloom(self, center: QPointF, radius: float, value: str, alpha: int) -> QRadialGradient:
        """A soft colored light source used to lift corners and hovered tiles."""
        gradient = QRadialGradient(center, max(1.0, radius))
        hot = QColor(value)
        hot.setAlpha(max(0, min(255, round(alpha * self.intensity))))
        cool = QColor(value)
        cool.setAlpha(0)
        gradient.setColorAt(0.0, hot)
        gradient.setColorAt(1.0, cool)
        return gradient

    def brand_gradient(self, rect: QRectF, *, hover: float = 0.0) -> QLinearGradient:
        """Primary-action fill: brand into accent, warming slightly on hover."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        start = mix(self.brand, self.brand_hover, hover * 0.85)
        end = mix(self.accent, self.brand, 0.18)
        end = mix(end, self.accent, hover * 0.5)
        gradient.setColorAt(0.0, QColor(start))
        gradient.setColorAt(1.0, QColor(mix(start, end, self.intensity)))
        return gradient

    def surface_gradient(self, rect: QRectF, *, lift: float = 0.0) -> QLinearGradient:
        """Card fill: a barely-there vertical sheen so tiles read as physical."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        top = mix(self.surface, self.glow_a, 0.16 * self.intensity + lift * 0.1)
        gradient.setColorAt(0.0, QColor(top))
        gradient.setColorAt(0.55, QColor(self.surface))
        gradient.setColorAt(1.0, QColor(mix(self.surface, self.surface_soft, 0.55 * self.intensity)))
        return gradient

    def sidebar_gradient(self, rect: QRectF) -> QLinearGradient:
        """Navigation rail: surface at the top melting into brand at the base."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(mix(self.surface, self.glow_a, 0.35 * self.intensity)))
        gradient.setColorAt(0.55, QColor(self.surface))
        gradient.setColorAt(1.0, QColor(mix(self.surface, self.brand_soft, 0.85 * self.intensity)))
        return gradient

    # ── customization ───────────────────────────────────────────────────────

    def _readable(self, value: str, foreground: str) -> str:
        """Nearest lightness to ``value`` that is legible both ways.

        A brand color has to clear 4.5:1 twice over: once as text on
        ``surface``, and once as the backdrop for ``foreground`` inside a
        filled button. Hue and saturation are the user's choice and are kept;
        lightness is searched outward from theirs so the result still looks
        like the color they picked.
        """
        original = _lightness(value)
        candidates = sorted((step / 100 for step in range(6, 96)), key=lambda l: abs(l - original))
        for candidate in candidates:
            trial = _at_lightness(value, candidate)
            if (
                contrast_ratio(trial, self.surface) >= 4.55
                and contrast_ratio(foreground, trial) >= 4.55
            ):
                return trial
        return self.brand

    def with_accent(self, accent: str) -> "StudioTokens":
        """Rebuild the brand family around a user-chosen color.

        The chosen hue is honoured; its lightness is not. Studio would rather
        show a slightly different shade of the requested color than an
        interface whose buttons cannot be read.
        """
        chosen = QColor(accent)
        if not chosen.isValid():
            return self
        # Filled controls always pair a light palette with white text and a
        # dark palette with near-black; picking the opposite is what produced
        # unreadable buttons for mid-bright accents.
        foreground = "#FFFFFF" if not self.dark else mix(self.canvas, "#000000", 0.45)
        base = self._readable(_hex(chosen), foreground)
        secondary = self._readable(_shift(base, hue=34.0, saturation=1.05), foreground)
        soft = mix(self.surface, base, 0.16 if not self.dark else 0.3)
        if contrast_ratio(base, soft) < 4.5:
            soft = mix(self.surface, base, 0.08 if not self.dark else 0.42)
        return replace(
            self,
            brand=base,
            brand_hover=_shift(base, lightness=0.88 if not self.dark else 1.12),
            brand_soft=soft,
            accent=secondary,
            glow_a=mix(self.canvas, base, 0.22 if not self.dark else 0.3),
            on_brand=foreground,
        )

    def with_intensity(self, intensity: float) -> "StudioTokens":
        """Return the same palette with decorative gradients dialled up or down."""
        return replace(self, intensity=max(0.0, min(1.0, intensity)))

    def with_density(self, scale: float) -> "StudioTokens":
        """Scale corner radii, so users can pick between soft and squared UI."""
        scale = max(0.0, min(1.6, scale))
        return replace(
            self,
            radius_sm=max(2, round(10 * scale)),
            radius_md=max(3, round(16 * scale)),
            radius_lg=max(4, round(24 * scale)),
        )

    def without_motion(self) -> "StudioTokens":
        """Reduced-motion variant: state still changes, nothing animates."""
        return replace(self, motion_fast=0, motion_normal=0, motion_slow=0)


TOKENS = StudioTokens()

PALETTES: dict[str, StudioTokens] = {
    "Aurora": TOKENS,
    "Graphite": StudioTokens(
        name="Graphite", canvas="#F2F3F6", glow_a="#E2E7F7", glow_b="#EDEAF3", glow_c="#E3EFF1",
        surface="#FFFFFF", surface_soft="#E9EBF0", surface_hover="#E0E4EC", ink="#20242B",
        ink_soft="#565D68", ink_faint="#82899A", brand="#3F57B8", brand_hover="#31479F",
        brand_soft="#E0E5FA", accent="#2F77C2", on_brand="#FFFFFF", coral="#C7614D",
        mint="#22766D", danger="#B54359", border="#D9DCE3", border_strong="#C4C8D2",
        shadow="#252A36",
    ),
    "Sage": StudioTokens(
        name="Sage", canvas="#F2F5F0", glow_a="#DEEDE1", glow_b="#EFF1DF", glow_c="#DDEDEC",
        surface="#FFFFFF", surface_soft="#E8EEE8", surface_hover="#DFE9E1", ink="#1F2B24",
        ink_soft="#52605A", ink_faint="#7F8B83", brand="#37634F", brand_hover="#2A4D3E",
        brand_soft="#DCEADF", accent="#537E4E", on_brand="#FFFFFF", coral="#B96852",
        mint="#2F6B5C", danger="#A93F4D", border="#D7E0D7", border_strong="#C3D0C4",
        shadow="#243329",
    ),
    "Sunset": StudioTokens(
        name="Sunset", canvas="#FBF5F1", glow_a="#F9E1D6", glow_b="#F7E7D2", glow_c="#EFE1EC",
        surface="#FFFFFF", surface_soft="#F4E9E2", surface_hover="#F1DFD6", ink="#30231F",
        ink_soft="#68584F", ink_faint="#95847A", brand="#A84432", brand_hover="#8C3527",
        brand_soft="#F7DED5", accent="#B65B2A", on_brand="#FFFFFF", coral="#CF654C",
        mint="#2F6F63", danger="#A83B4B", border="#E7D8CF", border_strong="#D8C2B6",
        shadow="#432D27",
    ),
    "Ocean": StudioTokens(
        name="Ocean", canvas="#F1F6F9", glow_a="#D9EDF6", glow_b="#E2F1EC", glow_c="#DFE6F7",
        surface="#FFFFFF", surface_soft="#E6EFF4", surface_hover="#DAEAF2", ink="#132029",
        ink_soft="#4C5C67", ink_faint="#7B8B96", brand="#186081", brand_hover="#114C68",
        brand_soft="#D7EAF3", accent="#1B8080", on_brand="#FFFFFF", coral="#C2604A",
        mint="#1F7A6B", danger="#B03F55", border="#D5E2EA", border_strong="#BFD1DC",
        shadow="#16303D",
    ),
    "Blossom": StudioTokens(
        name="Blossom", canvas="#FBF3F7", glow_a="#F8DEEC", glow_b="#F1E2F7", glow_c="#FBE6E0",
        surface="#FFFFFF", surface_soft="#F5E7EE", surface_hover="#F2DDE8", ink="#2A1B24",
        ink_soft="#65525C", ink_faint="#95818C", brand="#A83A76", brand_hover="#8C2C60",
        brand_soft="#F8DEEC", accent="#CC406D", on_brand="#FFFFFF", coral="#CB6055",
        mint="#2E7A6C", danger="#B03B4F", border="#EBD9E3", border_strong="#DCC2D1",
        shadow="#3D2130",
    ),
    "Midnight": StudioTokens(
        name="Midnight", dark=True, canvas="#10131B", glow_a="#211C42", glow_b="#122733",
        glow_c="#2A1730", surface="#181D28", surface_soft="#222938", surface_hover="#2B3345",
        ink="#F5F6FA", ink_soft="#B7BDCA", ink_faint="#8E96A8", brand="#9B88FF",
        brand_hover="#B09FFF", brand_soft="#302B4D", accent="#E07AC6", on_brand="#151224",
        coral="#F18B73", mint="#63C7B7", danger="#F0788C", border="#333B4D",
        border_strong="#465166", shadow="#05070C",
    ),
    "Nebula": StudioTokens(
        name="Nebula", dark=True, canvas="#0C1017", glow_a="#181A3C", glow_b="#2B1436",
        glow_c="#0E2A34", surface="#141A25", surface_soft="#1D2534", surface_hover="#273043",
        ink="#F2F5FB", ink_soft="#AEB7C8", ink_faint="#87909F", brand="#5FC8F5",
        brand_hover="#7FD6F8", brand_soft="#173040", accent="#C77BF7", on_brand="#08141C",
        coral="#FF9A7A", mint="#5FD6B4", danger="#FF7D95", border="#2A3346",
        border_strong="#3C4860", shadow="#03060A",
    ),
    "Carbon": StudioTokens(
        name="Carbon", dark=True, canvas="#121212", glow_a="#1E2226", glow_b="#22201C",
        glow_c="#181F1E", surface="#1A1A1C", surface_soft="#232326", surface_hover="#2C2C31",
        ink="#F4F4F5", ink_soft="#B3B3B9", ink_faint="#8A8A92", brand="#E8A33D",
        brand_hover="#F2B558", brand_soft="#33291A", accent="#E2705C", on_brand="#1A1206",
        coral="#EE8A6E", mint="#63C79B", danger="#F0788C", border="#2E2E33",
        border_strong="#42424A", shadow="#000000",
    ),
}


def palette_names() -> list[str]:
    """Palette names in presentation order: light themes first, then dark."""

    return [name for name, item in PALETTES.items() if not item.dark] + [
        name for name, item in PALETTES.items() if item.dark
    ]


def palette_choices() -> list[str]:
    """Everything a theme picker offers, with the system option first."""

    return [AUTO_PALETTE, *palette_names()]


def system_is_dark() -> bool:
    """Whether the desktop is currently asking applications to render dark.

    Falls back to light when Qt cannot tell — an unknown scheme on a light
    desktop merely looks conservative, while guessing dark on one paints white
    text onto a white taskbar-coloured window.
    """
    from PySide6.QtGui import QGuiApplication

    application = QGuiApplication.instance()
    if application is None:
        return False
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is None:  # pragma: no cover - Qt older than 6.5
        return False
    return bool(scheme() == Qt.ColorScheme.Dark)


def palette(name: str | None) -> StudioTokens:
    """Resolve a stored palette name, including the system-following option."""

    chosen = str(name or "")
    if chosen == AUTO_PALETTE:
        return PALETTES[AUTO_DARK if system_is_dark() else AUTO_LIGHT]
    return PALETTES.get(chosen, TOKENS)


def application_stylesheet(tokens: StudioTokens = TOKENS) -> str:
    """Global Qt rules; bespoke animated components paint themselves.

    Selectors are anchored to widget classes rather than to bare properties so
    that a rule meant for a panel cannot cascade into every label inside it —
    the usual cause of stray borders in stylesheet-driven Qt UIs.
    """
    focus_ring = mix(tokens.brand, tokens.surface, 0.45)
    return f"""
        * {{
            font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            color: {tokens.ink};
        }}
        QMainWindow, QWidget#StudioRoot {{ background: {tokens.canvas}; }}
        QDialog {{ background: {tokens.canvas}; }}
        QLabel {{ background: transparent; border: 0; }}
        QToolTip {{
            background: {tokens.ink}; color: {tokens.canvas}; border: 0;
            padding: 7px 10px; border-radius: 8px;
        }}
        QLineEdit {{
            background: {tokens.surface}; border: 1px solid {tokens.border};
            border-radius: {tokens.radius_md}px; padding: 11px 16px;
            selection-background-color: {tokens.brand};
            selection-color: {tokens.on_brand}; font-size: 14px;
        }}
        QLineEdit:hover {{ border-color: {focus_ring}; }}
        QLineEdit:focus {{ border: 2px solid {tokens.brand}; padding: 10px 15px; }}
        QScrollArea {{ border: 0; background: transparent; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{
            background: transparent; width: 11px; margin: 8px 1px;
        }}
        QScrollBar::handle:vertical {{
            background: {tokens.border_strong}; min-height: 42px; border-radius: 5px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {tokens.brand}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            height: 0; background: transparent;
        }}
        QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 1px 8px; }}
        QScrollBar::handle:horizontal {{
            background: {tokens.border_strong}; min-width: 42px; border-radius: 5px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            width: 0; background: transparent;
        }}
        QProgressBar {{
            background: {tokens.surface_soft}; border: 0; border-radius: 4px;
            min-height: 8px; max-height: 8px; text-align: center;
        }}
        QProgressBar::chunk {{
            border-radius: 4px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {tokens.brand}, stop:1 {tokens.accent});
        }}
        QComboBox {{
            background: {tokens.surface}; border: 1px solid {tokens.border};
            border-radius: {tokens.radius_sm}px; padding: 9px 34px 9px 12px;
            min-height: 20px;
        }}
        QComboBox:hover {{ border-color: {tokens.brand}; }}
        QComboBox::drop-down {{ border: 0; width: 28px; }}
        QComboBox QAbstractItemView {{
            background: {tokens.surface}; border: 1px solid {tokens.border};
            selection-background-color: {tokens.brand_soft};
            selection-color: {tokens.ink}; padding: 6px; outline: 0;
        }}
        QSlider::groove:horizontal {{
            height: 6px; border-radius: 3px; background: {tokens.surface_soft};
        }}
        QSlider::sub-page:horizontal {{
            height: 6px; border-radius: 3px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {tokens.brand}, stop:1 {tokens.accent});
        }}
        QSlider::handle:horizontal {{
            width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
            background: {tokens.surface}; border: 2px solid {tokens.brand};
        }}
        QSplitter::handle {{ background: transparent; width: 8px; }}
        QTabWidget::pane {{ border: 0; background: transparent; top: -1px; }}
        QTabBar::tab {{
            background: transparent; color: {tokens.ink_soft}; border: 0;
            padding: 11px 16px; margin-right: 4px; font-weight: 600;
        }}
        QTabBar::tab:hover {{ color: {tokens.brand}; }}
        QTabBar::tab:selected {{
            background: {tokens.surface}; color: {tokens.brand};
            border-radius: {tokens.radius_sm}px;
        }}
        QCheckBox {{ spacing: 9px; color: {tokens.ink}; padding: 5px 0; }}
        QCheckBox::indicator {{ width: 18px; height: 18px; }}
        QCheckBox::indicator:unchecked {{
            background: {tokens.surface}; border: 1px solid {tokens.border_strong};
            border-radius: 5px;
        }}
        QCheckBox::indicator:hover {{ border: 1px solid {tokens.brand}; }}
        QCheckBox::indicator:checked {{
            border: 1px solid {tokens.brand}; border-radius: 5px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {tokens.brand}, stop:1 {tokens.accent});
        }}
        QMessageBox {{ background: {tokens.canvas}; }}
    """
