"""The Studio chat window: a local model with tools, and an approval gate.

The window's job is to make an agent legible. Every tool the model runs shows
up as its own row, and every change it wants to make appears as a card holding
the exact document that would be written, with Apply and Discard on it. Nothing
the model produces reaches disk without a click on that card.

Model work runs on the thread pool. The loop is a generator of events, so the
worker forwards each event as a Qt signal and the widgets are only ever built
on the GUI thread.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextOption
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..assistant import Assistant, AssistantEvent, PendingChange, suggestion_prompts
from .components import AnimatedButton
from .dialogs import Panel, clear_layout
from .tokens import TOKENS, StudioTokens

__all__ = ["AssistantDialog", "Bubble", "ChangeCard", "ToolRow"]


class Bubble(QFrame):
    """One message, painted so the speaker is obvious without a label."""

    def __init__(
        self, text: str, *, role: str, tokens: StudioTokens, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.role = role
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        colour = tokens.on_brand if role == "user" else tokens.ink
        label.setStyleSheet(f"color:{colour}; font-size:13px;")
        layout.addWidget(label)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(self.tokens.radius_md)
        if self.role == "user":
            painter.setBrush(self.tokens.brand_gradient(rect, hover=0.3))
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setBrush(self.tokens.surface_gradient(rect))
            painter.setPen(QPen(QColor(self.tokens.border), 1))
        painter.drawRoundedRect(rect, radius, radius)
        super().paintEvent(event)


class ToolRow(QLabel):
    """A one-line record of a tool the model ran."""

    def __init__(self, text: str, *, ok: bool, tokens: StudioTokens, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        colour = tokens.ink_faint if ok else tokens.danger
        self.setStyleSheet(f"color:{colour}; font-size:10px; padding:1px 4px;")


class ChangeCard(Panel):
    """A staged change, with the document it would write and the two buttons."""

    approved = Signal(str)
    rejected = Signal(str)

    def __init__(
        self, token: str, change: PendingChange, tokens: StudioTokens, parent: QWidget | None = None
    ) -> None:
        super().__init__(tokens, parent)
        self.token = token
        self.body.setContentsMargins(15, 13, 15, 13)
        self.body.setSpacing(9)
        # Hug the content: in a transcript the card must be as tall as what it
        # holds, not as tall as the space left over.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        title = QLabel(change.title, self)
        title.setWordWrap(True)
        title.setStyleSheet(f"color:{tokens.brand}; font-size:13px; font-weight:700;")
        self.body.addWidget(title)
        summary = QLabel(change.summary, self)
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        self.body.addWidget(summary)

        preview = QPlainTextEdit(change.preview, self)
        preview.setReadOnly(True)
        preview.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        preview.setFont(QFont("Cascadia Mono, Consolas, monospace", 9))
        preview.setFixedHeight(190 if len(change.preview) > 160 else 76)
        preview.setStyleSheet(
            f"background:{tokens.surface_soft}; color:{tokens.ink};"
            f"border:1px solid {tokens.border}; border-radius:{tokens.radius_sm}px;"
        )
        self.body.addWidget(preview)

        row = QHBoxLayout()
        row.addStretch(1)
        self.status = QLabel("Nothing has been saved yet.", self)
        self.status.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        row.insertWidget(0, self.status, 1)
        self.discard = AnimatedButton("Discard", variant="ghost", tokens=tokens, parent=self)
        self.discard.clicked.connect(lambda: self.rejected.emit(self.token))
        self.apply = AnimatedButton(
            "Apply", variant="primary", icon_text="✓", tokens=tokens, parent=self
        )
        self.apply.clicked.connect(lambda: self.approved.emit(self.token))
        row.addWidget(self.discard)
        row.addWidget(self.apply)
        self.body.addLayout(row)

    def settle(self, text: str, *, tone: str = "done") -> None:
        """Lock the card once the user has decided."""
        self.apply.setEnabled(False)
        self.discard.setEnabled(False)
        colour = self.tokens.mint if tone == "done" else self.tokens.ink_faint
        self.status.setStyleSheet(f"color:{colour}; font-size:10px; font-weight:600;")
        self.status.setText(text)


class AssistantDialog(QDialog):
    """Chat with the local model about the library, and let it change it."""

    #: Emitted when the user submits; the window runs the loop off the GUI
    #: thread and feeds events back through :meth:`handle_event`.
    question_started = Signal(str)
    approve_requested = Signal(str)
    reject_requested = Signal(str)

    def __init__(
        self, assistant: Assistant, tokens: StudioTokens = TOKENS, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.tokens = tokens
        self.busy = False
        self._cards: dict[str, ChangeCard] = {}
        self.setWindowTitle("Assistant")
        self.resize(880, 780)
        self.setMinimumSize(620, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel("Assistant", self)
        font = QFont(heading.font())
        font.setPointSize(18)
        font.setWeight(QFont.Weight.DemiBold)
        heading.setFont(font)
        titles.addWidget(heading)
        self.subtitle = QLabel("Runs on your local model. Nothing leaves this machine.", self)
        self.subtitle.setStyleSheet(f"color:{tokens.ink_soft}; font-size:11px;")
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        reset = AnimatedButton("New chat", variant="ghost", icon_text="↺", tokens=tokens, parent=self)
        reset.clicked.connect(self.new_chat)
        header.addWidget(reset)
        outer.addLayout(header)

        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.host = QWidget(self.area)
        self.transcript = QVBoxLayout(self.host)
        self.transcript.setContentsMargins(2, 2, 10, 2)
        self.transcript.setSpacing(9)
        self.area.setWidget(self.host)
        outer.addWidget(self.area, 1)

        self.suggestions = QHBoxLayout()
        self.suggestions.setSpacing(7)
        outer.addLayout(self.suggestions)

        composer = QHBoxLayout()
        composer.setSpacing(9)
        self.input = QPlainTextEdit(self)
        self.input.setPlaceholderText(
            "Ask for a new way to sort your library, or about what is already in it…"
        )
        self.input.setFixedHeight(74)
        self.input.setStyleSheet(
            f"background:{tokens.surface}; border:1px solid {tokens.border};"
            f"border-radius:{tokens.radius_md}px; padding:10px 12px; font-size:13px;"
        )
        composer.addWidget(self.input, 1)
        self.send = AnimatedButton("Send", variant="primary", icon_text="➤", tokens=tokens, parent=self)
        self.send.setFixedHeight(74)
        self.send.clicked.connect(self.submit)
        composer.addWidget(self.send)
        outer.addLayout(composer)

        self.footer = QLabel(
            "The assistant can add and run filters. It cannot write program code, delete "
            "anything, or modify your original files.",
            self,
        )
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet(f"color:{tokens.ink_faint}; font-size:10px;")
        outer.addWidget(self.footer)

        self._show_welcome()

    # ── composing ───────────────────────────────────────────────────────────

    def _show_welcome(self) -> None:
        clear_layout(self.transcript)
        clear_layout(self.suggestions)
        self._cards.clear()
        self.transcript.addStretch(1)
        self._add(
            Bubble(
                "I can look through your library and add new ways to sort it.\n\n"
                "Ask for a filter — “sort my photos by French landmark”, “separate "
                "gameplay from IRL” — and I will look at what you actually have, write "
                "the filter, and show it to you before anything is saved.",
                role="assistant",
                tokens=self.tokens,
            )
        )
        for label, prompt in suggestion_prompts():
            chip = AnimatedButton(label, variant="chip", tokens=self.tokens, parent=self)
            chip.clicked.connect(lambda _=False, text=prompt: self._use_suggestion(text))
            self.suggestions.addWidget(chip)
        self.suggestions.addStretch(1)

    def _use_suggestion(self, text: str) -> None:
        self.input.setPlainText(text)
        self.submit()

    def _add(self, widget: QWidget) -> None:
        # Insert before the trailing stretch, so the conversation stacks from
        # the top instead of the widgets sharing the empty space between them.
        self.transcript.insertWidget(max(0, self.transcript.count() - 1), widget)
        QTimer.singleShot(30, self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        bar = self.area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def new_chat(self) -> None:
        if self.busy:
            return
        self.assistant.reset()
        self._show_welcome()

    # ── the conversation ────────────────────────────────────────────────────

    def submit(self) -> None:
        question = self.input.toPlainText().strip()
        if not question or self.busy:
            return
        clear_layout(self.suggestions)
        self.input.clear()
        self._add(Bubble(question, role="user", tokens=self.tokens))
        self._set_busy(True)
        self.status_row = ToolRow("Thinking…", ok=True, tokens=self.tokens)
        self._add(self.status_row)
        self.question_started.emit(question)

    def handle_event(self, event: AssistantEvent) -> None:
        """Render one event from the loop. Always on the GUI thread."""
        if event.kind == "thinking":
            if event.text:
                self._add(Bubble(event.text, role="assistant", tokens=self.tokens))
        elif event.kind == "tool":
            detail = ", ".join(f"{k}={str(v)[:40]}" for k, v in event.arguments.items())
            self.status_row = ToolRow(
                f"▸ {event.tool}({detail})", ok=True, tokens=self.tokens
            )
            self._add(self.status_row)
        elif event.kind == "tool_result":
            mark = "✓" if event.ok else "✕"
            self._add(
                ToolRow(
                    f"   {mark} {event.tool} — {event.text[:220]}",
                    ok=event.ok,
                    tokens=self.tokens,
                )
            )
        elif event.kind == "pending" and event.change is not None:
            card = ChangeCard(event.token, event.change, self.tokens, self.host)
            card.approved.connect(self._approve)
            card.rejected.connect(self._reject)
            self._cards[event.token] = card
            self._add(card)
        elif event.kind == "message":
            self._add(Bubble(event.text or "(no reply)", role="assistant", tokens=self.tokens))
        elif event.kind == "error":
            frame = Bubble(f"⚠  {event.text}", role="assistant", tokens=self.tokens)
            self._add(frame)

    def finish(self) -> None:
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send.setEnabled(not busy)
        self.send.setText("Working…" if busy else "Send")
        self.subtitle.setText(
            "Working — the model is thinking and running tools…"
            if busy
            else f"Running on {self.assistant.model or 'your local model'}."
        )

    # ── approvals ───────────────────────────────────────────────────────────

    def _approve(self, token: str) -> None:
        self.approve_requested.emit(token)

    def _reject(self, token: str) -> None:
        card = self._cards.get(token)
        if card is not None:
            card.settle("Discarded. Nothing was saved.", tone="muted")
        self.reject_requested.emit(token)

    def change_applied(self, token: str, detail: str) -> None:
        card = self._cards.get(token)
        if card is not None:
            card.settle(detail)

    def change_failed(self, token: str, detail: str) -> None:
        card = self._cards.get(token)
        if card is not None:
            card.status.setStyleSheet(f"color:{self.tokens.danger}; font-size:10px;")
            card.status.setText(detail[:160])
