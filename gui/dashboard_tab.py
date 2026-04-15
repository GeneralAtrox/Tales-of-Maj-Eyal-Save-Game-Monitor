from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from gui.enemy_panel import EnemyPanel
from gui.memory_reader import MemoryReader
from gui.theme import BORDER, GREEN, RED, SUBTEXT0, SURFACE1, TEXT


class DashboardTab(QWidget):
    """Character roster table with per-row action dropdown buttons."""

    character_selected    = Signal(str)   # folder_name — open Characters tab
    open_sheet_requested  = Signal(str)   # folder_name
    force_sync_requested  = Signal(str)   # folder_name
    restore_requested     = Signal(str, str)  # folder_name, backup_name
    _enemies_ready        = Signal(list)  # list[EntityInfo] from bg thread

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

        self._game_dot = QLabel("● Game Inactive")
        self._game_dot.setProperty("status", "error")
        header_row.addWidget(self._game_dot)

        self._hp_label = QLabel("")
        self._hp_label.setStyleSheet(f"font-weight: 600; color: {SUBTEXT0};")
        header_row.addWidget(self._hp_label)

        root.addLayout(header_row)

        # ── Memory reader (attaches in background thread) ──
        self._reader = MemoryReader()
        self._attach_pending = False
        self._hp_fail_count = 0       # consecutive None reads
        self._last_level_id: str | None = None   # map-change detection

        # ── Poll for ToME process every 3 s ──
        self._game_poll = QTimer(self)
        self._game_poll.setInterval(3000)
        self._game_poll.timeout.connect(self._check_game_process)
        self._game_poll.start()
        self._check_game_process()   # immediate first check

        # ── Poll HP every 1 s (fast — just a few ReadProcessMemory calls) ──
        self._hp_poll = QTimer(self)
        self._hp_poll.setInterval(1000)
        self._hp_poll.timeout.connect(self._poll_hp)
        self._hp_poll.start()

        # ── Poll level ID every 2 s for map-change detection ──
        self._level_poll = QTimer(self)
        self._level_poll.setInterval(2000)
        self._level_poll.timeout.connect(self._poll_level_id)
        self._level_poll.start()

        # ── Splitter: roster table (top) + enemy panel (bottom) ──
        splitter = QSplitter(Qt.Orientation.Vertical)

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
        splitter.addWidget(self._table)

        # ── Enemy panel ──
        self._enemy_panel = EnemyPanel()
        self._enemies_ready.connect(self._enemy_panel.update_enemies)
        splitter.addWidget(self._enemy_panel)

        splitter.setStretchFactor(0, 2)   # table gets more space
        splitter.setStretchFactor(1, 3)   # enemy panel gets rest
        root.addWidget(splitter)

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

    def set_game_status(self, active: bool) -> None:
        if active:
            self._game_dot.setText("● Game Active")
            self._game_dot.setProperty("status", "ok")
        else:
            self._game_dot.setText("● Game Inactive")
            self._game_dot.setProperty("status", "error")
        self._game_dot.style().unpolish(self._game_dot)
        self._game_dot.style().polish(self._game_dot)

    def _check_game_process(self) -> None:
        try:
            result = subprocess.run(
                ["tasklist"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            active = "t-engine" in result.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            active = False
        self.set_game_status(active)

        # Try to attach memory reader when game becomes active
        if active and not self._reader.attached and not self._attach_pending:
            self._attach_pending = True
            self._hp_label.setText("Attaching...")
            threading.Thread(target=self._attach_reader, daemon=True).start()
        elif not active and self._reader.attached:
            self._reader.detach()
            self._hp_label.setText("")

    def _attach_reader(self) -> None:
        """Run in background thread — the initial _G scan takes a few seconds."""
        try:
            self._reader.attach()
        finally:
            self._attach_pending = False

    def _poll_hp(self) -> None:
        if not self._reader.attached:
            return
        hp = self._reader.read_player_hp()
        if hp is not None:
            self._hp_fail_count = 0
            life, max_life = hp
            pct = life / max_life if max_life > 0 else 0
            if pct > 0.5:
                color = GREEN
            elif pct > 0.25:
                color = "#f9e2af"  # yellow
            else:
                color = RED
            self._hp_label.setStyleSheet(f"font-weight: 600; color: {color};")
            self._hp_label.setText(f"HP: {life:.0f} / {max_life:.0f}")
        else:
            self._hp_fail_count += 1
            # After 5 consecutive failures (5 s), _G is probably stale —
            # detach so the next game-poll cycle triggers a fresh re-attach.
            if self._hp_fail_count >= 5 and not self._attach_pending:
                self._reader.detach()
                self._hp_label.setText("")
                self._hp_fail_count = 0
            elif self._hp_fail_count >= 2:
                self._hp_label.setStyleSheet(f"font-weight: 600; color: {SUBTEXT0};")
                self._hp_label.setText("HP: --")

    def _poll_level_id(self) -> None:
        """Check for map change every 2 s — triggers entity scan on change."""
        if not self._reader.attached:
            return
        level_id = self._reader.read_level_id()
        if level_id is None:
            return
        if level_id != self._last_level_id:
            self._last_level_id = level_id
            self._enemy_panel.set_map_name(level_id)
            # Scan entities in background thread to avoid blocking the GUI
            threading.Thread(target=self._scan_entities, daemon=True).start()

    def _scan_entities(self) -> None:
        """Read entities (background thread) and emit signal for main thread."""
        entities = self._reader.read_entities(min_rank=1.5)
        self._enemies_ready.emit(entities)

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

    def _action_button(self, folder_name: str) -> QWidget:
        btn = QToolButton()
        btn.setText("Actions  ▾")
        btn.setFixedWidth(105)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setStyleSheet(
            "QToolButton { text-align: center; }"
            "QToolButton::menu-indicator { image: none; }"
        )
        menu = QMenu(btn)
        # Rebuild the backup list every time the menu opens
        menu.aboutToShow.connect(lambda: self._rebuild_menu(menu, folder_name))
        btn.setMenu(menu)

        # Wrap in a container so the button sits flush with the same
        # horizontal padding the text cells use (QTableWidget adds ~8 px
        # left padding; match it on the right so the button doesn't butt
        # up against the scrollbar/edge).
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(4, 0, 8, 0)
        lay.setSpacing(0)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        return container

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
