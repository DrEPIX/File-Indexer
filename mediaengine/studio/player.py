"""An in-app video player whose timeline knows what is in the video.

Opening a clip in an external player loses the one thing the library actually
knows: *where* in the video each label was seen. The analyzer records a
timestamp with every claim, so the scrub bar can show them — a mark at every
tagged moment, a list beside the picture, and a click that jumps straight
there.

The playback surface is Qt Multimedia, which on Windows means the platform's
own decoders. That is a deliberate limit rather than a shortcoming: shipping a
codec stack would dwarf the rest of the application, and anything Windows will
not play is one button away from the player the user already has.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QMouseEvent, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .components import AnimatedButton
from .tokens import TOKENS, StudioTokens

__all__ = ["Moment", "VideoPlayerDialog", "TagTimeline", "format_timecode", "group_moments"]

#: Two claims less than this far apart describe the same moment. Keyframes are
#: seconds apart, so anything closer is the same frame seen by two labels.
MOMENT_TOLERANCE_S = 0.75


def format_timecode(milliseconds: float) -> str:
    """``h:mm:ss`` for long videos, ``m:ss`` for short ones."""
    total = max(0, int(milliseconds // 1000))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


@dataclass(frozen=True, slots=True)
class Moment:
    """One point in a video, and everything the analyzers saw there."""

    seconds: float
    labels: tuple[str, ...]
    confidence: float

    @property
    def timecode(self) -> str:
        return format_timecode(self.seconds * 1000)

    @property
    def summary(self) -> str:
        return ", ".join(self.labels)


def group_moments(
    rows: Iterable[dict[str, Any]], *, tolerance_s: float = MOMENT_TOLERANCE_S
) -> list[Moment]:
    """Collapse timestamped annotations into one entry per instant.

    A frame tagged with twelve labels is one moment in the video, not twelve.
    Kept as a plain function over dictionaries so the grouping — the part with
    the off-by-one risks — is testable without a window, a player, or a GPU.
    """
    timed: list[tuple[float, str, float]] = []
    for row in rows:
        raw = row.get("frame_time")
        if raw is None:
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        try:
            confidence = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        timed.append((max(0.0, seconds), label, confidence))

    moments: list[Moment] = []
    for seconds, label, confidence in sorted(timed, key=lambda item: item[0]):
        if moments and abs(seconds - moments[-1].seconds) <= tolerance_s:
            previous = moments[-1]
            if label in previous.labels:
                continue
            moments[-1] = Moment(
                seconds=previous.seconds,
                labels=(*previous.labels, label),
                confidence=max(previous.confidence, confidence),
            )
            continue
        moments.append(Moment(seconds=seconds, labels=(label,), confidence=confidence))
    return moments


class TagTimeline(QWidget):
    """A scrub bar with a mark at every moment the analyzers labelled."""

    seek_requested = Signal(float)

    def __init__(self, tokens: StudioTokens = TOKENS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.moments: list[Moment] = []
        self.duration_s = 0.0
        self.position_s = 0.0
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def set_moments(self, moments: Sequence[Moment]) -> None:
        self.moments = list(moments)
        self.update()

    def set_duration(self, seconds: float) -> None:
        self.duration_s = max(0.0, seconds)
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position_s = max(0.0, seconds)
        self.update()

    # ── geometry ────────────────────────────────────────────────────────────

    def _track(self) -> QRectF:
        return QRectF(8.0, self.height() / 2 - 3.0, max(1.0, self.width() - 16.0), 6.0)

    def fraction_at(self, x: float) -> float:
        track = self._track()
        return min(1.0, max(0.0, (x - track.left()) / max(1.0, track.width())))

    def seconds_at(self, x: float) -> float:
        return self.fraction_at(x) * self.duration_s

    def moment_near(self, x: float, *, within_px: float = 7.0) -> Moment | None:
        """The labelled moment under the pointer, for tooltips and clicks."""
        if not self.moments or self.duration_s <= 0:
            return None
        track = self._track()
        best: tuple[float, Moment] | None = None
        for moment in self.moments:
            centre = track.left() + track.width() * min(1.0, moment.seconds / self.duration_s)
            distance = abs(centre - x)
            if distance <= within_px and (best is None or distance < best[0]):
                best = (distance, moment)
        return best[1] if best else None

    # ── interaction ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.duration_s <= 0:
            return
        # Snapping to a nearby marker matters: the marks are the reason to
        # click here at all, and a two-pixel miss that lands you 40 seconds
        # away feels broken rather than approximate.
        moment = self.moment_near(event.position().x())
        target = moment.seconds if moment else self.seconds_at(event.position().x())
        self.seek_requested.emit(target)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        moment = self.moment_near(event.position().x())
        if moment is not None:
            self.setToolTip(f"{moment.timecode} — {moment.summary}")
        else:
            self.setToolTip(format_timecode(self.seconds_at(event.position().x()) * 1000))

    # ── painting ────────────────────────────────────────────────────────────

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self._track()
        radius = track.height() / 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.tokens.surface_soft))
        painter.drawRoundedRect(track, radius, radius)

        if self.duration_s > 0:
            played = QRectF(track)
            played.setWidth(track.width() * min(1.0, self.position_s / self.duration_s))
            painter.setBrush(self.tokens.brand_gradient(played if played.width() > 1 else track))
            painter.drawRoundedRect(played, radius, radius)

            for moment in self.moments:
                centre = track.left() + track.width() * min(1.0, moment.seconds / self.duration_s)
                # Confidence sets the height, so a glance at the bar shows
                # where the model was sure and where it was hedging.
                height = 9.0 + 7.0 * min(1.0, max(0.0, moment.confidence))
                mark = QRectF(centre - 1.5, track.center().y() - height / 2, 3.0, height)
                colour = QColor(self.tokens.accent)
                colour.setAlpha(150 + int(90 * min(1.0, max(0.0, moment.confidence))))
                painter.setBrush(colour)
                painter.drawRoundedRect(mark, 1.5, 1.5)

            handle = QPointF(
                track.left() + track.width() * min(1.0, self.position_s / self.duration_s),
                track.center().y(),
            )
            painter.setBrush(QColor(self.tokens.surface))
            painter.setPen(QPen(QColor(self.tokens.brand), 2))
            painter.drawEllipse(handle, 7.0, 7.0)


class VideoPlayerDialog(QDialog):
    """Play one video, with its tagged moments on the timeline and beside it."""

    open_externally_requested = Signal(dict)

    def __init__(
        self,
        item: dict[str, Any],
        moments: Sequence[Moment],
        tokens: StudioTokens = TOKENS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.tokens = tokens
        self.setWindowTitle(str(item.get("filename") or "Video"))
        self.resize(1120, 700)
        self.setMinimumSize(720, 460)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        stage = QWidget(self)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(14, 14, 14, 12)
        stage_layout.setSpacing(10)

        self.video = QVideoWidget(stage)
        self.video.setMinimumHeight(260)
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        stage_layout.addWidget(self.video, 1)

        self.timeline = TagTimeline(tokens, stage)
        self.timeline.set_moments(moments)
        self.timeline.seek_requested.connect(self.seek_to)
        stage_layout.addWidget(self.timeline)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.play_button = AnimatedButton(
            "Pause", variant="primary", icon_text="❚❚", tokens=tokens, parent=stage
        )
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)
        for text, offset in (("−10s", -10.0), ("+10s", 10.0)):
            skip = AnimatedButton(text, variant="ghost", tokens=tokens, parent=stage)
            skip.clicked.connect(lambda _checked=False, delta=offset: self.nudge(delta))
            controls.addWidget(skip)
        self.clock = QLabel("0:00 / 0:00", stage)
        self.clock.setStyleSheet(f"color:{tokens.ink_soft}; font-size:12px;")
        controls.addWidget(self.clock)
        controls.addStretch(1)
        self.now_playing = QLabel("", stage)
        self.now_playing.setStyleSheet(f"color:{tokens.brand}; font-size:12px; font-weight:600;")
        controls.addWidget(self.now_playing)
        external = AnimatedButton("Open in default player", variant="ghost", tokens=tokens, parent=stage)
        external.clicked.connect(lambda: self.open_externally_requested.emit(self.item))
        controls.addWidget(external)
        stage_layout.addLayout(controls)
        outer.addWidget(stage, 1)

        side = QWidget(self)
        side.setFixedWidth(268)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(12, 16, 14, 14)
        side_layout.setSpacing(8)
        heading = QLabel("WHAT'S IN THIS VIDEO", side)
        heading.setStyleSheet(
            f"color:{tokens.ink_faint}; font-size:9px; font-weight:800; letter-spacing:1px;"
        )
        side_layout.addWidget(heading)
        self.count_label = QLabel("", side)
        self.count_label.setWordWrap(True)
        self.count_label.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        side_layout.addWidget(self.count_label)
        self.moment_list = QListWidget(side)
        self.moment_list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: 0; }}"
            f"QListWidget::item {{ padding: 7px 6px; border-radius: {tokens.radius_sm}px; }}"
            f"QListWidget::item:selected {{ background: {tokens.brand_soft}; color: {tokens.ink}; }}"
            f"QListWidget::item:hover {{ background: {tokens.surface_hover}; }}"
        )
        self.moment_list.itemActivated.connect(self._jump_to_item)
        self.moment_list.itemClicked.connect(self._jump_to_item)
        side_layout.addWidget(self.moment_list, 1)
        outer.addWidget(side)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.errorOccurred.connect(self._playback_failed)
        self.set_moments(moments)

        path = str(item.get("path") or "")
        if path:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()

    # ── moments ─────────────────────────────────────────────────────────────

    def set_moments(self, moments: Sequence[Moment]) -> None:
        self.moments = list(moments)
        self.timeline.set_moments(self.moments)
        self.moment_list.clear()
        for moment in self.moments:
            entry = QListWidgetItem(f"{moment.timecode}   {moment.summary}")
            entry.setData(Qt.ItemDataRole.UserRole, moment.seconds)
            entry.setToolTip(moment.summary)
            self.moment_list.addItem(entry)
        if self.moments:
            labels = {label for moment in self.moments for label in moment.labels}
            self.count_label.setText(
                f"{len(self.moments)} tagged moments, {len(labels)} distinct labels. "
                "Click one to jump there."
            )
        else:
            self.count_label.setText(
                "No timestamped tags yet. Run the local tagger over this video from the "
                "AI Model Store and its labels will appear here."
            )

    def _jump_to_item(self, entry: QListWidgetItem) -> None:
        seconds = entry.data(Qt.ItemDataRole.UserRole)
        if isinstance(seconds, (int, float)):
            self.seek_to(float(seconds))

    # ── transport ───────────────────────────────────────────────────────────

    def seek_to(self, seconds: float) -> None:
        self.player.setPosition(int(max(0.0, seconds) * 1000))
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()
        self._sync_play_button()

    def nudge(self, delta_s: float) -> None:
        self.seek_to(max(0.0, self.player.position() / 1000 + delta_s))

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()
        self._sync_play_button()

    def _sync_play_button(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("Pause" if playing else "Play")
        self.play_button.icon_text = "❚❚" if playing else "▶"
        self.play_button.update()

    def current_moment(self, seconds: float) -> Moment | None:
        """The most recent labelled moment at or before ``seconds``."""
        found: Moment | None = None
        for moment in self.moments:
            if moment.seconds <= seconds + 0.01:
                found = moment
            else:
                break
        return found

    # ── player signals ──────────────────────────────────────────────────────

    def _position_changed(self, milliseconds: int) -> None:
        seconds = milliseconds / 1000
        self.timeline.set_position(seconds)
        self.clock.setText(
            f"{format_timecode(milliseconds)} / {format_timecode(self.player.duration())}"
        )
        moment = self.current_moment(seconds)
        self.now_playing.setText(moment.summary if moment else "")

    def _duration_changed(self, milliseconds: int) -> None:
        self.timeline.set_duration(milliseconds / 1000)

    def _playback_failed(self, error: Any, text: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        # Windows plays what Windows has codecs for. Rather than pretend
        # otherwise, say so and hand the file to whatever the user already uses.
        self.count_label.setText(
            f"This file could not be played here ({text or error}). "
            "Use “Open in default player” — your tagged moments are listed below either way."
        )

    def closeEvent(self, event: Any) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
        elif key == Qt.Key.Key_Left:
            self.nudge(-5.0)
        elif key == Qt.Key.Key_Right:
            self.nudge(5.0)
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


def open_externally(item: dict[str, Any]) -> bool:
    """Hand a file to the desktop's own player."""
    path = Path(str(item.get("path") or ""))
    if not path.exists():
        return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
