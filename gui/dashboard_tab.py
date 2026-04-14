from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.theme import BORDER, GREEN, RED, SUBTEXT0, SURFACE1, TEXT, YELLOW


class DashboardTab(QWidget):
    """Character roster table with per-row action dropdown buttons."""

    character_selected = Signal(str)   # folder_name — open in Characters tab
    force_sync_requested = Signal(str) # folder_name
    open_sheet_requested = Signal(str) # folder_name

    _COL_NAME      = 0
    _COL_CLASS     = 1
    _COL_LEVEL     = 2
    _COL_LAST_SAVE = 3
    _COL_ACTIONS   = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ── Header row ──
        header_row = QHBoxLayout()

        title = QLabel("Monitor Dashboard")
        title.setProperty("heading", True)
        header_row.addWidget(title)
        header_row.addStretch()

        self._status_dot = QLabel("● Initializing")
        self._status_dot.setProperty("status", "warn")
        header_row.addWidget(self._status_dot)

        root.addLayout(header_row)

        # ── Roster table ──
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Character", "Class / Race", "Level", "Last Save", "Actions"]
        )
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(self._COL_NAME,      QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(self._COL_CLASS,     QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(self._COL_LEVEL,     QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(self._COL_LAST_SAVE, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(self._COL_ACTIONS,   QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(self._COL_LEVEL,     70)
        self._table.setColumnWidth(self._COL_LAST_SAVE, 145)
        self._table.setColumnWidth(self._COL_ACTIONS,   110)

        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.itemDoubleClicked.connect(self._on_double_click)

        root.addWidget(self._table)

        self._rows: dict[str, int] = {}  # folder_name → row index

    # ── Public API ────────────────────────────────────────────────────────

    def set_monitor_status(self, active: bool) -> None:
        if active:
            self._status_dot.setText("● Monitor Active")
            self._status_dot.setProperty("status", "ok")
        else:
            self._status_dot.setText("● Stopped")
            self._status_dot.setProperty("status", "error")
        # Force Qt to re-evaluate the dynamic property stylesheet
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)

    def upsert_character(
        self,
        folder_name: str,
        name: str,
        class_race: str,
        level: str,
        last_save: str,
    ) -> None:
        """Insert a new row or update an existing one for *folder_name*."""
        if folder_name in self._rows:
            row = self._rows[folder_name]
        else:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[folder_name] = row
            self._table.setCellWidget(row, self._COL_ACTIONS, self._action_button(folder_name))
            self._table.setRowHeight(row, 38)

        for col, text in (
            (self._COL_NAME,      name),
            (self._COL_CLASS,     class_race),
            (self._COL_LEVEL,     level),
            (self._COL_LAST_SAVE, last_save),
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            item.setData(Qt.ItemDataRole.UserRole, folder_name)
            self._table.setItem(row, col, item)

    # ── Internals ─────────────────────────────────────────────────────────

    def _action_button(self, folder_name: str) -> QToolButton:
        btn = QToolButton()
        btn.setText("Actions  ▾")
        btn.setFixedWidth(105)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu(btn)
        menu.addAction("View Characters", lambda: self.character_selected.emit(folder_name))
        menu.addAction("View Sheet",      lambda: self.open_sheet_requested.emit(folder_name))
        menu.addSeparator()
        menu.addAction("Force Sync",      lambda: self.force_sync_requested.emit(folder_name))
        btn.setMenu(menu)
        return btn

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        folder_name = item.data(Qt.ItemDataRole.UserRole)
        if folder_name:
            self.character_selected.emit(folder_name)
