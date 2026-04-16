from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QToolButton,
    QWidget,
    QWidgetAction,
    QLabel,
)

from gui.bridge import InputBridge, LogBridge, MonitorThread
from gui.dashboard_tab import DashboardTab
from gui.log_panel import LogPanel
from gui.settings_tab import SettingsTab
from gui.theme import TEXT


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("TOME Save Monitor")
        self.resize(1280, 760)
        self.setMinimumSize(900, 560)

        # ── Log bridge: redirect stdout/stderr before anything prints ──
        self._log_bridge = LogBridge(self)
        self._log_bridge.install()

        # ── Input bridge ──
        self._input_bridge = InputBridge(self)
        self._input_bridge.input_needed.connect(self._handle_input_request)

        # ── Log panel (parented into dashboard's splitter) ──
        self._log_panel = LogPanel()

        # ── Tab widget fills the whole window ──
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        # ── Corner widget: character dropdown + Actions button ──────────────
        corner = QWidget()
        corner_lay = QHBoxLayout(corner)
        corner_lay.setContentsMargins(4, 2, 8, 2)
        corner_lay.setSpacing(6)

        self._char_combo = QComboBox()
        self._char_combo.setMinimumWidth(200)
        self._char_combo.setPlaceholderText("Select character…")
        corner_lay.addWidget(self._char_combo)

        self._actions_btn = QToolButton()
        self._actions_btn.setText("Actions  \u25be")
        self._actions_btn.setFixedWidth(105)
        self._actions_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._actions_btn.setStyleSheet(
            "QToolButton { text-align: center; }"
            "QToolButton::menu-indicator { image: none; }"
        )
        self._actions_menu = QMenu(self._actions_btn)
        self._actions_menu.aboutToShow.connect(self._rebuild_actions_menu)
        self._actions_btn.setMenu(self._actions_menu)
        corner_lay.addWidget(self._actions_btn)

        self._tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)

        # ── Status bar ──
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Starting\u2026")

        # ── Dashboard tab (log panel parented inside it) ──
        self._dashboard = DashboardTab(log_panel=self._log_panel)
        self._tabs.addTab(self._dashboard, "Monitor")

        self._settings_tab: SettingsTab | None = None

        # ── Wire signals ──
        self._log_bridge.message_ready.connect(self._log_panel.append)
        self._char_combo.currentIndexChanged.connect(self._on_char_combo_changed)
        self._dashboard.analyze_requested.connect(self._run_analysis)

        # ── Start monitor thread ──
        self._monitor = MonitorThread(config_path, self._input_bridge)
        self._monitor.start()

        # ── Poll until initialize_system finishes ──
        self._init_poll = QTimer(self)
        self._init_poll.setInterval(400)
        self._init_poll.timeout.connect(self._check_init)
        self._init_poll.start()

        self._watcher: QFileSystemWatcher | None = None

    # ── Init polling ───────────────────────────────────────────────────────

    def _check_init(self) -> None:
        config = self._monitor.config
        if config is None:
            return

        self._init_poll.stop()
        self._dashboard.set_monitor_status(active=True)
        self._dashboard.set_roots(config.character_sheets_root, config.backup_root)
        self.statusBar().showMessage("Monitor active")

        # Settings tab
        self._settings_tab = SettingsTab()
        self._settings_tab.load_config(config)
        self._settings_tab.config_saved.connect(self._on_config_saved)
        self._tabs.addTab(self._settings_tab, "Settings")

        # Populate character combo
        for char in config.characters:
            class_race, level = self._read_sheet_meta(
                config.character_sheets_root / f"data_{char.folder_name}.json"
            )
            self._dashboard.add_character(char.folder_name, char.name)
            self._char_combo.addItem(
                self._char_label(char.name, class_race, level),
                userData=char.folder_name,
            )

        # Auto-select first character
        if self._char_combo.count() > 0:
            self._char_combo.setCurrentIndex(0)

        # Watch for sheet updates
        config.character_sheets_root.mkdir(exist_ok=True)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(str(config.character_sheets_root))
        self._watcher.directoryChanged.connect(self._on_sheets_changed)

    # ── Signal handlers ────────────────────────────────────────────────────

    def _on_char_combo_changed(self, index: int) -> None:
        folder_name = self._char_combo.itemData(index)
        if folder_name:
            self._dashboard.select_character(folder_name)

    def _on_sheets_changed(self, _path: str) -> None:
        config = self._monitor.config
        if not config:
            return
        # Refresh combo labels with updated level info
        for i in range(self._char_combo.count()):
            folder_name = self._char_combo.itemData(i)
            char = next((c for c in config.characters if c.folder_name == folder_name), None)
            if char:
                class_race, level = self._read_sheet_meta(
                    config.character_sheets_root / f"data_{char.folder_name}.json"
                )
                self._char_combo.setItemText(
                    i, self._char_label(char.name, class_race, level)
                )
        self._dashboard.refresh_current()
        self.statusBar().showMessage("Character sheet updated", 4000)

    def _rebuild_actions_menu(self) -> None:
        self._actions_menu.clear()
        folder_name = self._char_combo.currentData()
        config = self._monitor.config

        if not folder_name or not config:
            no_char = self._actions_menu.addAction("No character selected")
            no_char.setEnabled(False)
            return

        self._actions_menu.addAction(
            "Force Sync",
            lambda fn=folder_name: self._force_sync(fn),
        )
        self._actions_menu.addSeparator()

        # Restore Save header (non-clickable label)
        header_lbl = QLabel("  Restore Save")
        header_lbl.setStyleSheet(
            f"font-weight: 700; color: {TEXT}; padding: 5px 12px 3px 12px;"
        )
        header_action = QWidgetAction(self._actions_menu)
        header_action.setDefaultWidget(header_lbl)
        self._actions_menu.addAction(header_action)

        backups = self._get_backups(folder_name, config.backup_root)
        if backups:
            for backup_path in backups:
                label = "  " + self._format_backup_name(backup_path.name)
                action = self._actions_menu.addAction(label)
                action.triggered.connect(
                    lambda checked=False, fn=folder_name, bn=backup_path.name:
                        self._restore_backup(fn, bn)
                )
        else:
            no_backup = self._actions_menu.addAction("  No backups available")
            no_backup.setEnabled(False)

    def _force_sync(self, folder_name: str) -> None:
        config = self._monitor.config
        if not config:
            return
        for char in config.characters:
            if char.folder_name == folder_name:
                from te4_client import schedule_scrying_sync
                schedule_scrying_sync(char, config)
                self.statusBar().showMessage(f"Sync scheduled for {char.name}", 3000)
                return

    def _on_config_saved(self, config: object) -> None:
        from monitor import save_config
        from models import AppConfig
        if isinstance(config, AppConfig):
            save_config(config)
            self.statusBar().showMessage("Config saved", 3000)

    def _restore_backup(self, folder_name: str, backup_name: str) -> None:
        config = self._monitor.config
        if not config:
            return
        char_name = next(
            (c.name for c in config.characters if c.folder_name == folder_name),
            folder_name,
        )
        reply = QMessageBox.warning(
            self,
            "Restore Save File",
            f"Restore <b>{char_name}</b> to backup <b>{backup_name}</b>?<br><br>"
            "This will <b>overwrite the current save</b> on disk and cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        backup_path = config.backup_root / folder_name / backup_name
        try:
            from backups import restore_backup
            restore_backup(backup_path, config.save_root, folder_name)
            print(f"[*] {char_name} restored from {backup_name}.")
            self.statusBar().showMessage(f"Restored {char_name} from {backup_name}", 5000)
        except OSError as exc:
            QMessageBox.critical(self, "Restore Failed", str(exc))

    def _run_analysis(self, folder_name: str, question: str) -> None:  # noqa: ARG002
        self._dashboard.set_analysis_result(
            "Claude API integration not yet configured.\n\n"
            "Wire up gui/main_window.py _run_analysis() to the Anthropic SDK\n"
            "using the system prompt from agent.md and the sheet JSON as the\n"
            "user message."
        )

    def _handle_input_request(self, prompt: str) -> None:
        text, ok = QInputDialog.getText(self, "Input Required", prompt)
        self._input_bridge.provide(text if ok else "")

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _char_label(name: str, class_race: str, level: str) -> str:
        if class_race and level:
            return f"{name}  \u2014  {class_race}  Lv {level}"
        if level:
            return f"{name}  Lv {level}"
        return name

    @staticmethod
    def _get_backups(folder_name: str, backups_root: Path) -> list[Path]:
        backup_dir = backups_root / folder_name
        if not backup_dir.exists():
            return []
        return sorted(
            (p for p in backup_dir.iterdir() if p.is_dir()),
            reverse=True,
        )

    @staticmethod
    def _format_backup_name(name: str) -> str:
        try:
            parts = name.split("_")
            dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
            return dt.strftime("%b %d, %Y  %I:%M %p")
        except (IndexError, ValueError):
            return name

    @staticmethod
    def _read_sheet_meta(sheet_path: Path) -> tuple[str, str]:
        if not sheet_path.exists():
            return "", ""
        try:
            data = json.loads(sheet_path.read_text(encoding="utf-8"))
            ch = data.get("Character", {})
            cls  = ch.get("Class", "")
            race = ch.get("Race", "")
            class_race = " / ".join(filter(None, [cls, race]))
            level = ch.get("Level / Exp", "").split(" ")[0]
            return class_race, level
        except (OSError, json.JSONDecodeError, AttributeError):
            return "", ""

    # ── Cleanup ────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._log_bridge.uninstall()
        super().closeEvent(event)
