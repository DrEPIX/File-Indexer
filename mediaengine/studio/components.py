"""Reusable animated components for File Indexer Studio.

Every widget in here paints itself rather than leaning on a Qt stylesheet.
That is deliberate: stylesheet rules cascade into child widgets, which is how
a single ``border: 1px solid`` on a panel ends up drawing a box around every
label inside it.  Owner-drawn widgets also let a palette carry gradients,
blooms, and hover glows that stylesheets cannot express.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .tokens import TOKENS, StudioTokens, mix

__all__ = [
    "MEDIA_LABELS",
    "AnimatedButton",
    "AssetCard",
    "CheckBox",
    "EmptyState",
    "GradientCanvas",
    "InspectorPanel",
    "PreviewSurface",
    "StatusDock",
    "TagStrip",
    "Toast",
    "human_duration",
    "human_size",
    "open_item",
]


def _mix(left: QColor, right: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor(
        round(left.red() + (right.red() - left.red()) * amount),
        round(left.green() + (right.green() - left.green()) * amount),
        round(left.blue() + (right.blue() - left.blue()) * amount),
        round(left.alpha() + (right.alpha() - left.alpha()) * amount),
    )


def human_size(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return "Size unknown"
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "Size unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "Size unknown"


def human_duration(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return ""
    try:
        seconds = max(0, round(float(value or 0)))
    except (TypeError, ValueError):
        return ""
    if not seconds:
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


MEDIA_LABELS = {
    "image": ("Photo", "▣"),
    "video": ("Video", "▶"),
    "audio": ("Audio", "♪"),
    "document": ("Document", "▤"),
    "other": ("File", "◆"),
}


class GradientCanvas(QWidget):
    """The window backdrop: a diagonal wash plus two soft corner blooms.

    Painting the background here instead of in a stylesheet is what makes the
    gradient survive palette changes, resizing, and reduced-motion mode without
    any of the widgets stacked on top needing to know it exists.
    """

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setObjectName("StudioRoot")
        self.setAutoFillBackground(False)

    def set_tokens(self, tokens: StudioTokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.fillRect(rect, self.tokens.canvas_gradient(rect))
        if self.tokens.intensity <= 0.01:
            return
        painter.fillRect(
            rect,
            self.tokens.bloom(
                QPointF(rect.width() * 0.12, rect.height() * 0.04),
                rect.width() * 0.55,
                self.tokens.brand,
                34,
            ),
        )
        painter.fillRect(
            rect,
            self.tokens.bloom(
                QPointF(rect.width() * 0.96, rect.height() * 0.92),
                rect.width() * 0.5,
                self.tokens.accent,
                28,
            ),
        )


class AnimatedButton(QAbstractButton):
    """Rounded owner-drawn button with hover, press, and keyboard states."""

    def __init__(
        self,
        text: str,
        *,
        variant: str = "secondary",
        icon_text: str = "",
        tokens: StudioTokens = TOKENS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self.variant = variant
        self.icon_text = icon_text
        self.tokens = tokens
        self._hover_progress = 0.0
        self._press_progress = 0.0
        self._keyboard_focus = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(42 if variant != "chip" else 36)
        self._hover_animation = self._animation(b"hoverProgress", tokens.motion_normal)
        self._press_animation = self._animation(b"pressProgress", tokens.motion_fast)

    def _animation(self, prop: bytes, duration: int) -> QPropertyAnimation:
        animation = QPropertyAnimation(self, prop, self)
        animation.setDuration(duration)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        return animation

    def set_tokens(self, tokens: StudioTokens) -> None:
        """Adopt a new palette in place so live theme changes need no rebuild."""
        self.tokens = tokens
        self._hover_animation.setDuration(tokens.motion_normal)
        self._press_animation.setDuration(tokens.motion_fast)
        self.setMinimumHeight(42 if self.variant != "chip" else 36)
        self.update()

    def sizeHint(self) -> QSize:
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        icon_width = metrics.horizontalAdvance(self.icon_text) + 9 if self.icon_text else 0
        horizontal = 36 if self.variant == "primary" else 30
        width = metrics.horizontalAdvance(self.text()) + icon_width + horizontal
        return QSize(max(74, width), self.minimumHeight())

    def _get_hover(self) -> float:
        return self._hover_progress

    def _set_hover(self, value: float) -> None:
        self._hover_progress = value
        self.update()

    hoverProgress = Property(float, _get_hover, _set_hover)

    def _get_press(self) -> float:
        return self._press_progress

    def _set_press(self, value: float) -> None:
        self._press_progress = value
        self.update()

    pressProgress = Property(float, _get_press, _set_press)

    def _animate(
        self,
        animation: QPropertyAnimation,
        setter: Callable[[float], None],
        start: float,
        end: float,
    ) -> None:
        animation.stop()
        if animation.duration() <= 0:
            # Reduced motion: jump to the end state rather than animating to it.
            setter(end)
            return
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.start()

    def _hover_to(self, end: float) -> None:
        self._animate(self._hover_animation, self._set_hover, self._hover_progress, end)

    def _press_to(self, end: float) -> None:
        self._animate(self._press_animation, self._set_press, self._press_progress, end)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover_to(0.0)
        self._press_to(0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press_to(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press_to(0.0)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -2.5)
        tokens = self.tokens
        brand = QColor(tokens.brand)
        surface = QColor(tokens.surface)
        soft = QColor(tokens.surface_soft)
        hover = QColor(tokens.brand_soft)
        ink = QColor(tokens.ink)
        muted = QColor(tokens.ink_soft)
        radius = rect.height() / 2 if self.variant == "chip" else float(tokens.radius_sm)
        fill: QBrush | QColor
        checked = self.isCheckable() and self.isChecked() and self.isEnabled()

        if not self.isEnabled():
            fill, foreground, outline = soft, QColor(tokens.ink_faint), QColor(tokens.border)
        elif self.variant == "primary":
            self._paint_glow(painter, rect, radius)
            fill = QBrush(tokens.brand_gradient(rect, hover=self._hover_progress))
            foreground, outline = QColor(tokens.on_brand), QColor(0, 0, 0, 0)
        elif self.variant == "ghost":
            fill = _mix(QColor(255, 255, 255, 0), hover, self._hover_progress)
            foreground = _mix(muted, brand, self._hover_progress)
            outline = QColor(0, 0, 0, 0)
        elif self.variant == "danger":
            danger = QColor(tokens.danger)
            fill = _mix(surface, danger, 0.08 + self._hover_progress * 0.12)
            foreground = danger
            outline = _mix(QColor(tokens.border), danger, 0.35 + self._hover_progress * 0.45)
        elif self.variant == "chip":
            fill = _mix(surface, hover, self._hover_progress)
            foreground = ink
            outline = _mix(QColor(tokens.border), brand, self._hover_progress)
        else:
            fill = _mix(soft, hover, self._hover_progress)
            foreground = ink
            outline = _mix(QColor(tokens.border), brand, self._hover_progress * 0.65)

        if checked and self.variant != "primary":
            sheen = QLinearGradient(rect.topLeft(), rect.bottomRight())
            sheen.setColorAt(0.0, QColor(mix(tokens.surface, tokens.brand, 0.18)))
            sheen.setColorAt(1.0, QColor(mix(tokens.surface, tokens.accent, 0.2)))
            fill = QBrush(sheen)
            foreground = QColor(tokens.brand)
            outline = _mix(QColor(tokens.border), brand, 0.75)

        if self.variant == "primary" and self.isEnabled():
            rect.translate(0, -1 - self._hover_progress + self._press_progress * 1.5)
        painter.setBrush(fill)
        painter.setPen(QPen(outline, 1.3) if outline.alpha() else Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, radius, radius)

        if self.hasFocus() and self._keyboard_focus:
            focus_rect = QRectF(rect).adjusted(2.5, 2.5, -2.5, -2.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tokens.mint), 2))
            painter.drawRoundedRect(focus_rect, max(5.0, radius - 2), max(5.0, radius - 2))

        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(foreground)
        label = f"{self.icon_text}  {self.text()}" if self.icon_text else self.text()
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_glow(self, painter: QPainter, rect: QRectF, radius: float) -> None:
        """Cast a brand-tinted drop shadow that intensifies on hover."""
        alpha = round((36 + self._hover_progress * 34) * (0.35 + 0.65 * self.tokens.intensity))
        for step in range(3):
            spread = 1.0 + step * 1.6 + self._hover_progress * 1.4
            shadow = QRectF(rect).adjusted(-spread, spread * 0.35, spread, spread * 1.35)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.tokens.shadow_color(max(4, alpha // (step + 2))))
            painter.drawRoundedRect(shadow, radius + spread, radius + spread)

    def focusInEvent(self, event: Any) -> None:
        self._keyboard_focus = event.reason() in {
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        }
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self._keyboard_focus = False
        self.update()
        super().focusOutEvent(event)


class CheckBox(QCheckBox):
    """A checkbox that draws its own tick.

    Qt stylesheets can only put a *picture* inside ``::indicator``, so a
    styled checkbox without a bundled image asset is a coloured square with no
    tick in it — legible, but it reads as a swatch rather than a control.
    Painting the indicator keeps the gradient and gets a real checkmark.
    """

    def __init__(self, text: str, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.tokens = tokens
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0, (self.height() - 18) / 2, 18, 18)
        radius = 5.0
        if self.isChecked():
            painter.setPen(QPen(QColor(self.tokens.brand), 1.2))
            painter.setBrush(QBrush(self.tokens.brand_gradient(box, hover=0.4)))
        else:
            painter.setPen(QPen(QColor(self.tokens.border_strong), 1.4))
            painter.setBrush(QColor(self.tokens.surface))
        painter.drawRoundedRect(box, radius, radius)
        if self.isChecked():
            tick = QPainterPath()
            tick.moveTo(box.left() + 4.4, box.top() + 9.2)
            tick.lineTo(box.left() + 7.6, box.top() + 12.6)
            tick.lineTo(box.left() + 13.6, box.top() + 5.6)
            pen = QPen(QColor(self.tokens.on_brand), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(tick)
        painter.setPen(QColor(self.tokens.ink if self.isEnabled() else self.tokens.ink_faint))
        painter.drawText(
            QRectF(box.right() + 9, 0, self.width() - box.right() - 9, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.text(),
        )

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self.text()) + 34, max(26, metrics.height() + 10))


class PreviewSurface(QWidget):
    """Rounded image surface with pointer-responsive depth and light."""

    def __init__(self, media_type: str, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.media_type = media_type if media_type in MEDIA_LABELS else "other"
        self.pixmap = QPixmap()
        self.tilt = QPointF(0.0, 0.0)
        self.shine = QPointF(0.5, 0.5)
        self.setMinimumHeight(158)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.video_widget: QWidget | None = None

    def set_thumbnail(self, path: str | Path | None) -> None:
        pixmap = QPixmap(str(path)) if path else QPixmap()
        self.pixmap = pixmap if not pixmap.isNull() else QPixmap()
        self.update()

    def set_tilt(self, x: float, y: float) -> None:
        self.tilt = QPointF(max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))
        self.shine = QPointF((self.tilt.x() + 1) / 2, (self.tilt.y() + 1) / 2)
        self.update()

    def reset_tilt(self) -> None:
        self.tilt = QPointF()
        self.shine = QPointF(0.5, 0.5)
        self.update()

    def attach_video_widget(self, widget: QWidget) -> None:
        self.video_widget = widget
        widget.setParent(self)
        widget.setGeometry(self.rect().adjusted(1, 1, -1, -1))
        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        widget.hide()

    def resizeEvent(self, event: QResizeEvent) -> None:
        if self.video_widget is not None:
            self.video_widget.setGeometry(self.rect().adjusted(1, 1, -1, -1))
        super().resizeEvent(event)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self.tokens.radius_md, self.tokens.radius_md)
        painter.setClipPath(path)
        if not self.pixmap.isNull():
            self._paint_photo(painter, rect)
        else:
            self._paint_placeholder(painter, rect)
        overlay = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        overlay.setColorAt(0.55, QColor(0, 0, 0, 0))
        overlay.setColorAt(1.0, QColor(18, 15, 31, 92))
        painter.fillRect(rect, overlay)
        if self.tilt.manhattanLength() > 0.02:
            center = QPointF(
                rect.left() + rect.width() * self.shine.x(),
                rect.top() + rect.height() * self.shine.y(),
            )
            painter.fillRect(rect, self.tokens.bloom(center, rect.width() * 0.72, "#FFFFFF", 44))

    def _paint_photo(self, painter: QPainter, rect: QRectF) -> None:
        source_ratio = self.pixmap.width() / max(1, self.pixmap.height())
        target_ratio = rect.width() / max(1.0, rect.height())
        if source_ratio > target_ratio:
            crop_h = self.pixmap.height()
            crop_w = round(crop_h * target_ratio)
            overflow = self.pixmap.width() - crop_w
            crop_x = round(overflow * (0.5 + self.tilt.x() * 0.08))
            source = QRectF(crop_x, 0, crop_w, crop_h)
        else:
            crop_w = self.pixmap.width()
            crop_h = round(crop_w / target_ratio)
            overflow = self.pixmap.height() - crop_h
            crop_y = round(overflow * (0.5 + self.tilt.y() * 0.08))
            source = QRectF(0, crop_y, crop_w, crop_h)
        painter.save()
        center = rect.center()
        painter.translate(center)
        painter.rotate(self.tilt.x() * 1.35)
        scale = 1.015 + abs(self.tilt.y()) * 0.008
        painter.scale(scale, scale)
        painter.translate(-center)
        painter.drawPixmap(rect, self.pixmap, source)
        painter.restore()

    def _paint_placeholder(self, painter: QPainter, rect: QRectF) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(self.tokens.toned(self.tokens.glow_a)))
        gradient.setColorAt(0.5, QColor(self.tokens.surface_soft))
        gradient.setColorAt(1.0, QColor(self.tokens.toned(self.tokens.glow_b)))
        painter.fillRect(rect, gradient)
        painter.fillRect(
            rect,
            self.tokens.bloom(
                QPointF(rect.center().x(), rect.top() + rect.height() * 0.3),
                rect.width() * 0.7,
                self.tokens.brand,
                30,
            ),
        )
        label, glyph = MEDIA_LABELS[self.media_type]
        painter.setPen(QColor(self.tokens.brand))
        font = QFont(self.font())
        font.setPixelSize(30)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect.adjusted(0, -14, 0, -14), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.setPen(QColor(self.tokens.ink_soft))
        font.setPixelSize(11)
        painter.setFont(font)
        painter.drawText(rect.adjusted(0, 30, 0, 0), Qt.AlignmentFlag.AlignCenter, label)


class TagStrip(QWidget):
    """A compact row of pill-shaped labels, elided to fit the tile width.

    Analyzer output is the whole point of the product, so tiles show it inline
    instead of hiding every generated tag behind a selection.
    """

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.tags: list[str] = []
        self.setFixedHeight(19)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_tags(self, tags: list[str]) -> None:
        self.tags = [tag for tag in tags if tag][:6]
        self.setVisible(bool(self.tags))
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        if not self.tags:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPixelSize(9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        left = 0.0
        for index, tag in enumerate(self.tags):
            width = metrics.horizontalAdvance(tag) + 14
            if left + width > self.width():
                remaining = len(self.tags) - index
                if remaining > 0 and left + 26 <= self.width():
                    painter.setPen(QColor(self.tokens.ink_faint))
                    painter.drawText(
                        QRectF(left, 0, 26, self.height()),
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                        f"+{remaining}",
                    )
                return
            pill = QRectF(left, 1.0, width, self.height() - 2.0)
            gradient = QLinearGradient(pill.topLeft(), pill.bottomRight())
            gradient.setColorAt(0.0, QColor(mix(self.tokens.surface, self.tokens.brand, 0.14)))
            gradient.setColorAt(1.0, QColor(mix(self.tokens.surface, self.tokens.accent, 0.16)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(QColor(self.tokens.brand))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, tag)
            left += width + 5


class AssetCard(QWidget):
    """Friendly media tile with tilt, lift, selection, and video hover loop."""

    selected = Signal(dict)
    open_requested = Signal(dict)

    def __init__(
        self,
        item: dict[str, Any],
        tokens: StudioTokens = TOKENS,
        parent: QWidget | None = None,
        *,
        video_hover_enabled: bool = True,
        preferred_width: int = 245,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.tokens = tokens
        self.video_hover_enabled = video_hover_enabled
        self._hover_progress = 0.0
        self._selected = False
        self._hovering = False
        self._player: Any = None
        self._audio: Any = None
        self._video: Any = None
        self._loop_start = 0
        self._loop_end = 0
        self.entry_animation: QPropertyAnimation | None = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(max(190, preferred_width - 27))
        self.setMaximumWidth(preferred_width + 70)
        self.setMinimumHeight(282)
        self.setObjectName("AssetCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(7)
        media_type = str(item.get("media_type") or "other")
        self.preview = PreviewSurface(media_type, tokens, self)
        self.preview.set_thumbnail(item.get("thumbnail_path"))
        layout.addWidget(self.preview, 1)

        self.title_label = QLabel(str(item.get("filename") or "Untitled"), self)
        title_font = QFont(self.font())
        title_font.setPointSize(10)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(title_font)
        self.title_label.setToolTip(str(item.get("path") or item.get("filename") or ""))
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.title_label)

        self.tag_strip = TagStrip(tokens, self)
        self.tag_strip.set_tags([str(tag) for tag in (item.get("tags") or [])])
        layout.addWidget(self.tag_strip)

        meta = QHBoxLayout()
        meta.setSpacing(7)
        friendly, _ = MEDIA_LABELS.get(media_type, MEDIA_LABELS["other"])
        self.badge = QLabel(friendly.upper(), self)
        self.badge.setStyleSheet(self._badge_style())
        meta.addWidget(self.badge, 0)
        duration = human_duration(item.get("duration_s"))
        detail = duration or human_size(item.get("size_bytes"))
        self.meta_label = QLabel(detail, self)
        self.meta_label.setStyleSheet(f"color:{tokens.ink_soft}; font-size:10px;")
        meta.addWidget(self.meta_label, 0)
        meta.addStretch(1)
        dimensions = ""
        if item.get("width") and item.get("height"):
            dimensions = f"{item['width']}×{item['height']}"
        self.dimensions_label = QLabel(dimensions, self)
        self.dimensions_label.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        meta.addWidget(self.dimensions_label, 0)
        layout.addLayout(meta)

        self._hover_animation = QPropertyAnimation(self, b"hoverProgress", self)
        self._hover_animation.setDuration(tokens.motion_slow)
        self._hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._install_tracking(self)

    def _badge_style(self) -> str:
        return (
            f"background:{self.tokens.brand_soft}; color:{self.tokens.brand}; border-radius:7px;"
            "padding:3px 7px; font-size:9px; font-weight:700;"
        )

    def _install_tracking(self, widget: QWidget) -> None:
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def _get_hover(self) -> float:
        return self._hover_progress

    def _set_hover(self, value: float) -> None:
        self._hover_progress = value
        self.update()

    hoverProgress = Property(float, _get_hover, _set_hover)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def _drive_hover(self, target: float) -> None:
        self._hover_animation.stop()
        if self._hover_animation.duration() <= 0:
            self._set_hover(target)
            return
        self._hover_animation.setStartValue(self._hover_progress)
        self._hover_animation.setEndValue(target)
        self._hover_animation.start()

    def enterEvent(self, event: QEnterEvent) -> None:
        self._begin_hover()
        super().enterEvent(event)

    def _begin_hover(self) -> None:
        self._hovering = True
        self._drive_hover(1.0)
        if self.video_hover_enabled and self.item.get("media_type") == "video":
            QTimer.singleShot(180, self._start_video_preview)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovering = False
        self._drive_hover(0.0)
        self.preview.reset_tilt()
        QTimer.singleShot(120, self._stop_video_preview)
        super().leaveEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            if not self._hovering:
                self._begin_hover()
            local = (
                watched.mapTo(self, event.position().toPoint())
                if isinstance(watched, QWidget)
                else event.position().toPoint()
            )
            x = (local.x() / max(1, self.width()) - 0.5) * 2
            y = (local.y() / max(1, self.height()) - 0.5) * 2
            self.preview.set_tilt(x, y)
        elif event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton:
                self.selected.emit(self.item)
        elif event.type() == QEvent.Type.MouseButtonDblClick:
            self.open_requested.emit(self.item)
        return bool(super().eventFilter(watched, event))

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QRectF(self.rect()).adjusted(3, 3, -3, -5)
        base.translate(0, -self._hover_progress * 2.5)
        radius = float(self.tokens.radius_lg)

        if self._hover_progress > 0.02 or self._selected:
            weight = max(self._hover_progress, 0.7 if self._selected else 0.0)
            glow = QRectF(base).adjusted(-6, -3, 6, 9)
            painter.fillRect(
                glow,
                self.tokens.bloom(
                    QPointF(base.center().x(), base.center().y() + base.height() * 0.42),
                    base.width() * 0.85,
                    self.tokens.brand,
                    round(46 * weight),
                ),
            )

        shadow = QRectF(base).translated(self.preview.tilt.x() * 1.5, 2.5 + self.preview.tilt.y() * 1.2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.tokens.shadow_color(round(20 + self._hover_progress * 26)))
        painter.drawRoundedRect(shadow, radius, radius)

        painter.setBrush(QBrush(self.tokens.surface_gradient(base, lift=self._hover_progress)))
        if self._selected:
            border_brush: QBrush | QColor = QBrush(self.tokens.brand_gradient(base))
            width = 2.0
        else:
            border_brush = _mix(
                QColor(self.tokens.border), QColor(self.tokens.brand), self._hover_progress * 0.72
            )
            width = 1.0
        painter.setPen(QPen(border_brush, width))
        painter.drawRoundedRect(base, radius, radius)

    def _start_video_preview(self) -> None:
        if not self.video_hover_enabled or not self._hovering or self.item.get("media_type") != "video":
            return
        source = Path(str(self.item.get("path") or ""))
        if not source.is_file():
            return
        if self._player is None:
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
                from PySide6.QtMultimediaWidgets import QVideoWidget
            except ImportError:
                return
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setMuted(True)
            self._player.setAudioOutput(self._audio)
            self._video = QVideoWidget(self.preview)
            self._video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            self.preview.attach_video_widget(self._video)
            self._player.setVideoOutput(self._video)
            self._player.durationChanged.connect(self._duration_ready)
            self._player.positionChanged.connect(self._position_changed)
            self._player.setSource(QUrl.fromLocalFile(str(source)))
        if self._video is not None:
            self._video.show()
            self._video.raise_()
        if self._loop_start:
            self._player.setPosition(self._loop_start)
        self._player.play()

    def _duration_ready(self, duration: int) -> None:
        if duration <= 0 or self._player is None:
            return
        self._loop_start = round(duration * 0.12)
        self._loop_end = min(duration - 120, self._loop_start + 2600)
        self._player.setPosition(self._loop_start)

    def _position_changed(self, position: int) -> None:
        if self._loop_end and position >= self._loop_end and self._player is not None:
            self._player.setPosition(self._loop_start)

    def _stop_video_preview(self) -> None:
        if self._hovering:
            return
        if self._player is not None:
            self._player.pause()
        if self._video is not None:
            self._video.hide()


class InspectorPanel(QFrame):
    """Large preview and plain-language details for the selected asset."""

    open_requested = Signal(dict)
    reveal_requested = Signal(dict)
    favorite_requested = Signal(dict)

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.item: dict[str, Any] | None = None
        self.setObjectName("InspectorPanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(376)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        self.eyebrow = QLabel("QUICK LOOK", self)
        self.eyebrow.setStyleSheet(
            f"color:{tokens.brand}; font-size:10px; font-weight:800; letter-spacing:1px;"
        )
        layout.addWidget(self.eyebrow)
        self.preview = QLabel(self)
        self.preview.setMinimumHeight(196)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(self._preview_style())
        self.preview.setText("Select a tile\nto see more")
        layout.addWidget(self.preview)
        self.title = QLabel("Nothing selected", self)
        self.title.setWordWrap(True)
        title_font = QFont(self.font())
        title_font.setPointSize(14)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(title_font)
        layout.addWidget(self.title)
        self.summary = QLabel("Pick a photo, video, document, or audio file from your library.", self)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color:{tokens.ink_soft};")
        layout.addWidget(self.summary)
        self.ai_summary = QLabel("", self)
        self.ai_summary.setWordWrap(True)
        self.ai_summary.setStyleSheet(f"color:{tokens.ink};font-size:11px;")
        self.ai_summary.hide()
        layout.addWidget(self.ai_summary)
        self.tags = TagStrip(tokens, self)
        self.tags.hide()
        layout.addWidget(self.tags)
        self.path_label = QLabel("", self)
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        layout.addWidget(self.path_label)
        layout.addStretch(1)
        self.open_button = AnimatedButton("Open", variant="primary", icon_text="↗", tokens=tokens, parent=self)
        self.open_button.clicked.connect(self._open_current)
        layout.addWidget(self.open_button)
        row = QHBoxLayout()
        self.reveal_button = AnimatedButton("Show in folder", variant="secondary", tokens=tokens, parent=self)
        self.reveal_button.clicked.connect(self._reveal_current)
        self.favorite_button = AnimatedButton("Favorite", variant="secondary", icon_text="♡", tokens=tokens, parent=self)
        self.favorite_button.clicked.connect(self._favorite_current)
        row.addWidget(self.reveal_button, 1)
        row.addWidget(self.favorite_button, 1)
        layout.addLayout(row)
        self._set_actions_enabled(False)

    def _preview_style(self) -> str:
        return (
            f"background:{self.tokens.surface_soft}; border:0;"
            f"border-radius:{self.tokens.radius_md}px;"
            f"color:{self.tokens.ink_faint}; font-size:13px;"
        )

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self.tokens.radius_lg)
        painter.setBrush(QBrush(self.tokens.surface_gradient(rect)))
        painter.setPen(QPen(QColor(self.tokens.border), 1))
        painter.drawRoundedRect(rect, radius, radius)
        painter.fillRect(
            rect,
            self.tokens.bloom(
                QPointF(rect.center().x(), rect.top()), rect.width() * 0.9, self.tokens.brand, 26
            ),
        )
        super().paintEvent(event)

    def _open_current(self) -> None:
        if self.item is not None:
            self.open_requested.emit(self.item)

    def _reveal_current(self) -> None:
        if self.item is not None:
            self.reveal_requested.emit(self.item)

    def _favorite_current(self) -> None:
        if self.item is not None:
            self.favorite_requested.emit(self.item)

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.open_button.setEnabled(enabled)
        self.reveal_button.setEnabled(enabled)
        self.favorite_button.setEnabled(enabled)

    def show_item(self, item: dict[str, Any]) -> None:
        self.item = item
        self.title.setText(str(item.get("filename") or "Untitled"))
        media_type = str(item.get("media_type") or "other")
        friendly = MEDIA_LABELS.get(media_type, MEDIA_LABELS["other"])[0]
        facts = [friendly, human_size(item.get("size_bytes"))]
        duration = human_duration(item.get("duration_s"))
        if duration:
            facts.append(duration)
        if item.get("width") and item.get("height"):
            facts.append(f"{item['width']} × {item['height']}")
        self.summary.setText("  ·  ".join(facts))
        summary = str(item.get("ai_summary") or "").strip()
        self.ai_summary.setText(summary)
        self.ai_summary.setVisible(bool(summary))
        tags = [str(tag) for tag in (item.get("tags") or [])]
        self.tags.set_tags(tags)
        path = str(item.get("path") or "")
        self.path_label.setText(path)
        pixmap = QPixmap(str(item.get("thumbnail_path") or ""))
        if not pixmap.isNull():
            target = self.preview.size() - QSize(12, 12)
            self.preview.setPixmap(
                pixmap.scaled(
                    target,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.preview.setText("")
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText(f"{friendly} preview\nwill appear after indexing")
        self._set_actions_enabled(bool(path))


class EmptyState(QFrame):
    """The first-run invitation, painted as a gradient card."""

    add_folder_requested = Signal()

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 50, 40, 50)
        layout.setSpacing(12)
        icon = QLabel("✦", self)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"color:{tokens.brand}; font-size:36px;")
        layout.addWidget(icon)
        title = QLabel("Bring your library to life", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:650;")
        layout.addWidget(title)
        self.detail = QLabel(
            "Drop a folder anywhere here. Studio will index it, build previews, and keep it "
            "searchable automatically.",
            self,
        )
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setStyleSheet(f"color:{tokens.ink_soft}; font-size:13px;")
        layout.addWidget(self.detail)
        button = AnimatedButton("Choose a folder", variant="primary", icon_text="+", tokens=tokens, parent=self)
        button.clicked.connect(self.add_folder_requested)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_message(self, message: str) -> None:
        self.detail.setText(message)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self.tokens.radius_lg)
        painter.setBrush(QBrush(self.tokens.surface_gradient(rect)))
        painter.setPen(QPen(QColor(self.tokens.border), 1))
        painter.drawRoundedRect(rect, radius, radius)
        painter.fillRect(
            rect,
            self.tokens.bloom(
                QPointF(rect.center().x(), rect.top() + rect.height() * 0.25),
                rect.width() * 0.65,
                self.tokens.brand,
                30,
            ),
        )
        super().paintEvent(event)


class StatusDock(QFrame):
    """Persistent background-work readout with a breathing activity pulse."""

    cancel_requested = Signal()

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self._busy = False
        self._pulse = 0.0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 9, 10, 9)
        layout.setSpacing(11)
        self.dot = QWidget(self)
        self.dot.setFixedSize(10, 10)
        layout.addWidget(self.dot)
        self.label = QLabel("Library ready", self)
        self.label.setStyleSheet(f"color:{tokens.ink_soft};")
        layout.addWidget(self.label, 1)
        self.progress = QProgressBar(self)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(150)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.cancel = AnimatedButton("Cancel", variant="ghost", tokens=tokens, parent=self)
        self.cancel.clicked.connect(self.cancel_requested)
        self.cancel.hide()
        layout.addWidget(self.cancel)
        self._pulse_animation = QPropertyAnimation(self, b"pulse", self)
        self._pulse_animation.setDuration(max(1, tokens.motion_slow * 4))
        self._pulse_animation.setStartValue(0.0)
        self._pulse_animation.setEndValue(1.0)
        self._pulse_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_animation.setLoopCount(-1)

    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, value: float) -> None:
        self._pulse = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self.tokens.radius_md)
        painter.setBrush(QBrush(self.tokens.surface_gradient(rect)))
        painter.setPen(QPen(QColor(self.tokens.border), 1))
        painter.drawRoundedRect(rect, radius, radius)
        signal = QColor(self.tokens.coral if self._busy else self.tokens.mint)
        center = QPointF(self.dot.geometry().center())
        if self._busy:
            wave = abs(self._pulse * 2 - 1)
            painter.fillRect(rect, self.tokens.bloom(center, 26 + wave * 14, signal.name(), 90))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(signal)
        painter.drawEllipse(center, 4.0, 4.0)
        super().paintEvent(event)

    def set_ready(self, text: str = "Library ready") -> None:
        self._busy = False
        self._pulse_animation.stop()
        self.label.setText(text)
        self.progress.hide()
        self.cancel.hide()
        self.update()

    def set_busy(self, text: str, fraction: float | None = None) -> None:
        self.label.setText(text)
        if not self._busy:
            self._busy = True
            if self._pulse_animation.duration() > 4:
                self._pulse_animation.start()
        self.progress.show()
        self.cancel.show()
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(max(0.0, min(1.0, fraction)) * 1000))
        self.update()


class Toast(QLabel):
    """Transient confirmation, painted with the brand gradient."""

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(20, 12, 20, 12)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.hide()

    def set_tokens(self, tokens: StudioTokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -2.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.tokens.shadow_color(70))
        painter.drawRoundedRect(QRectF(rect).translated(0, 3), 14, 14)
        painter.setBrush(QBrush(self.tokens.brand_gradient(rect, hover=0.4)))
        painter.drawRoundedRect(rect, 14, 14)
        painter.setPen(QColor(self.tokens.on_brand))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())


def open_item(item: dict[str, Any]) -> bool:
    path = Path(str(item.get("path") or ""))
    return path.exists() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
