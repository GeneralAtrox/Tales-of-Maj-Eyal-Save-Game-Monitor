from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
from PySide6.QtWidgets import (
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from gui.bridge import InputBridge, LogBridge, MonitorThread
from gui.character_tab import CharacterTab
from gui.dashboard_tab import DashboardTab
from gui.log_panel import LogPanel
from gui.settings_tab import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("TOME Save Monitor")
        self.resize(1150, 700)
        self.setMinimumSize(820, 520)

        # ── Log bridge: redirect stdout/stderr before anything prints ──
        self._log_bridge = LogBridge(self)
        self._log_bridge.install()

        # ── Input bridge: routes monitor input() calls to QInputDialog ──
        self._input_bridge = InputBridge(self)
        self._input_bridge.input_needed.connect(self._handle_input_request)

        # ── Central layout: tab area | log panel ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        self._tabs = QTabWidget()
        splitter.addWidget(self._tabs)

        self._log_panel = LogPanel()
        self._log_panel.setFixedWidth(290)
        splitter.addWidget(self._log_panel)

        self.setCentralWidget(splitter)

        # ── Status bar ──
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Starting…")

        # ── Dashboard tab (always present) ──
        self._dashboard = DashboardTab()
        self._tabs.addTab(self._dashboard, "Monitor")

        # Characters and Settings tabs built once config is available
        self._character_tab: CharacterTab | None = None
        self._settings_tab: SettingsTab | None = None

        # ── Wire log bridge → log panel ──
        self._log_bridge.message_ready.connect(self._log_panel.append)

        # ── Wire dashboard signals ──
        self._dashboard.character_selected.connect(self._open_character_tab)
        self._dashboard.open_sheet_requested.connect(self._open_character_tab)
        self._dashboard.force_sync_requested.connect(self._force_sync)

        # ── Start monitor thread ──
        self._monitor = MonitorThread(config_path, self._input_bridge)
        self._monitor.start()

        # ── Poll until initialize_system finishes and config is ready ──
        self._init_poll = QTimer(self)
        self._init_poll.setInterval(400)
        self._init_poll.timeout.connect(self._check_init)
        self._init_poll.start()

        # FileSystemWatcher — set up after config loads
        self._watcher: QFileSystemWatcher | None = None

    # ── Init polling ──────────────────────────────────────────────────────

    def _check_init(self) -> None:
        config = self._monitor.config
        if config is None:
            return

        self._init_poll.stop()
        self._dashboard.set_monitor_status(active=True)
        self.statusBar().showMessage("Monitor active")

        # ── Build character-aware tabs now that config is known ──
        self._character_tab = CharacterTab(
            config.character_sheets_root,
            config.backup_root,
        )
        self._character_tab.analyze_requested.connect(self._run_analysis)
        self._character_tab.restore_requested.connect(self._restore_backup)
        self._tabs.addTab(self._character_tab, "Characters")

        self._settings_tab = SettingsTab()
        self._settings_tab.load_config(config)
        self._settings_tab.config_saved.connect(self._on_config_saved)
        self._tabs.addTab(self._settings_tab, "Settings")

        # ── Populate roster and character selector ──
        for char in config.characters:
            class_race, level = self._read_sheet_meta(
                config.character_sheets_root / f"data_{char.folder_name}.json"
            )
            self._dashboard.upsert_character(
                folder_name=char.folder_name,
                name=char.name,
                class_race=class_race,
                level=level,
                last_save="—",
            )
            self._character_tab.add_character(char.folder_name, char.name)

        # ── Watch CharacterSheets dir for vault sync updates ──
        config.character_sheets_root.mkdir(exist_ok=True)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(str(config.character_sheets_root))
        self._watcher.directoryChanged.connect(self._on_sheets_changed)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_sheets_changed(self, _path: str) -> None:
        """Refresh character display when a new sheet lands on disk."""
        config = self._monitor.config
        if not config or not self._character_tab:
            return

        # Update dashboard level/class from refreshed sheets
        for char in config.characters:
            class_race, level = self._read_sheet_meta(
                config.character_sheets_root / f"data_{char.folder_name}.json"
            )
            if class_race or level:
                self._dashboard.upsert_character(
                    folder_name=char.folder_name,
                    name=char.name,
                    class_race=class_race,
                    level=level,
                    last_save="just now",
                )

        self._character_tab.refresh_current()
        self.statusBar().showMessage("Character sheet updated", 4000)

    def _open_character_tab(self, folder_name: str) -> None:
        if not self._character_tab:
            return
        self._character_tab.select_character(folder_name)
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Characters":
                self._tabs.setCurrentIndex(i)
                break

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
            f"This will <b>overwrite the current save</b> on disk and cannot be undone.",
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

    def _run_analysis(self, folder_name: str, question: str) -> None:
        """Placeholder — wire up Claude API here."""
        if not self._character_tab:
            return
        self._character_tab.set_analysis_result(
            "Claude API integration not yet configured.\n\n"
            "Wire up gui/main_window.py _run_analysis() to the Anthropic SDK\n"
            "using the system prompt from agent.md and the sheet JSON as the\n"
            "user message."
        )

    def _handle_input_request(self, prompt: str) -> None:
        """Show a QInputDialog when the monitor thread calls input()."""
        text, ok = QInputDialog.getText(self, "Input Required", prompt)
        self._input_bridge.provide(text if ok else "")

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _read_sheet_meta(sheet_path: Path) -> tuple[str, str]:
        """Return (class/race string, level string) from a character sheet."""
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

    # ── Cleanup ───────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._log_bridge.uninstall()
        super().closeEvent(event)
