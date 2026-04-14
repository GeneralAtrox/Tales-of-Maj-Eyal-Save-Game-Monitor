from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
    QWidgetAction,
)

from gui.theme import BORDER, SUBTEXT0, SURFACE1, TEXT


class DashboardTab(QWidget):
    """Character roster table with per-row action dropdown buttons."""

    character_selected    = Signal(str)   # folder_name — open Characters tab
    open_sheet_requested  = Signal(str)   # folder_name
    force_sync_requested  = Signal(str)   # folder_name
    restore_requested     = Signal(str, str)  # folder_name, backup_name

    _COL_NAME      = 0
    _COL_CLASS     = 1
    _COL_LEVEL     = 2
    _COL_LAST_SAVE = 3
    _COL_ACTIONS   = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backups_root: Path | None = None

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

    def set_backups_root(self, path: Path) -> None:
        self._backups_root = path

    def set_monitor_status(self, active: bool) -> None:
        if active:
            self._status_dot.setText("● Monitor Active")
            self._status_dot.setProperty("status", "ok")
        else:
            self._status_dot.setText("● Stopped")
            self._status_dot.setProperty("status", "error")
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
        # Rebuild the backup list every time the menu opens
        menu.aboutToShow.connect(lambda: self._rebuild_menu(menu, folder_name))
        btn.setMenu(menu)
        return btn

    def _rebuild_menu(self, menu: QMenu, folder_name: str) -> None:
        menu.clear()

        # ── View section ──
        menu.addAction("View Character Sheet", lambda: self.character_selected.emit(folder_name))
        menu.addAction("View Sheet",           lambda: self.open_sheet_requested.emit(folder_name))

        menu.addSeparator()

        # ── Restore Save section header (bold, not clickable) ──
        header_lbl = QLabel("  Restore Save")
        header_lbl.setStyleSheet(
            f"font-weight: 700; color: {TEXT}; padding: 5px 12px 3px 12px;"
        )
        header_action = QWidgetAction(menu)
        header_action.setDefaultWidget(header_lbl)
        menu.addAction(header_action)

        # ── Backup items ──
        backups = self._get_backups(folder_name)
        if backups:
            for backup_path in backups:
                label = "  " + self._format_backup_name(backup_path.name)
                action = menu.addAction(label)
                # Capture backup_path.name by value
                action.triggered.connect(
                    lambda checked=False, bn=backup_path.name:
                        self.restore_requested.emit(folder_name, bn)
                )
        else:
            no_backup = menu.addAction("  No backups available")
            no_backup.setEnabled(False)

        menu.addSeparator()

        # ── Force Sync ──
        menu.addAction("Force Sync", lambda: self.force_sync_requested.emit(folder_name))

    def _get_backups(self, folder_name: str) -> list[Path]:
        if self._backups_root is None:
            return []
        backup_dir = self._backups_root / folder_name
        if not backup_dir.exists():
            return []
        return sorted(
            (p for p in backup_dir.iterdir() if p.is_dir()),
            reverse=True,  # newest first
        )

    @staticmethod
    def _format_backup_name(name: str) -> str:
        """Convert 'backup_20250414_143022' → 'Apr 14, 2025  2:30 PM'."""
        try:
            parts = name.split("_")  # ["backup", "20250414", "143022", ...]
            dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
            return dt.strftime("%b %d, %Y  %I:%M %p")
        except (IndexError, ValueError):
            return name

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        folder_name = item.data(Qt.ItemDataRole.UserRole)
        if folder_name:
            self.character_selected.emit(folder_name)
