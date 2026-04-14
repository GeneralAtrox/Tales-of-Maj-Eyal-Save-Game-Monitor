from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QWheelEvent
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from gui.theme import BLUE, BORDER, GREEN, MAUVE, RED, SUBTEXT0, SURFACE0, SURFACE1, TEXT, YELLOW

_LOG_FONT = "\"Cascadia Code\", \"Consolas\", \"Courier New\", monospace"


class LogPanel(QWidget):
    """Persistent vertical log panel.

    Appends colour-coded messages from the monitor threads.  The inner
    QPlainTextEdit consumes all wheel events so they never propagate to
    any parent scroll area.
    """

    MAX_LINES = 1_000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(210)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar ──
        header = QLabel("  OUTPUT")
        header.setFixedHeight(34)
        header.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        header.setStyleSheet(
            f"background: {SURFACE1};"
            f" color: {SUBTEXT0};"
            f" font-size: 11px;"
            f" font-weight: 700;"
            f" letter-spacing: 1.5px;"
            f" border-bottom: 1px solid {BORDER};"
            f" padding-left: 10px;"
        )
        layout.addWidget(header)

        # ── Log body ──
        self._edit = _WheelIsolatedEdit(self)
        self._edit.setReadOnly(True)
        self._edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: {SURFACE0};"
            f"  border: none;"
            f"  border-radius: 0;"
            f"  font-family: {_LOG_FONT};"
            f"  font-size: 12px;"
            f"  padding: 4px 6px;"
            f"}}"
        )
        layout.addWidget(self._edit)

    # ── Public API ────────────────────────────────────────────────────────

    def append(self, message: str) -> None:
        """Append a colour-coded message and auto-scroll to the bottom."""
        color = self._color_for(message)
        escaped = (
            message
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self._edit.appendHtml(f'<span style="color:{color};">{escaped}</span>')
        self._trim_excess()
        self._edit.moveCursor(QTextCursor.MoveOperation.End)

    def clear(self) -> None:
        self._edit.clear()

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _color_for(message: str) -> str:
        low = message.lower()
        if message.startswith("[!]") or "error" in low or "failed" in low:
            return RED
        if "anchored" in low or "success" in low or "verified" in low:
            return GREEN
        if message.startswith(" >") or "sync" in low or "scry" in low:
            return MAUVE
        if message.startswith("[*]") or "found" in low or "saved" in low:
            return YELLOW
        if "---" in message:
            return BLUE
        return TEXT

    def _trim_excess(self) -> None:
        doc = self._edit.document()
        while doc.blockCount() > self.MAX_LINES:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()  # remove trailing newline of deleted block


class _WheelIsolatedEdit(QPlainTextEdit):
    """QPlainTextEdit that accepts all wheel events so they never bubble up."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        super().wheelEvent(event)
        event.accept()
