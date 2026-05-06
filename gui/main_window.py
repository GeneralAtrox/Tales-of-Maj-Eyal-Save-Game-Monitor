from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from gui.bridge import InputBridge, LogBridge, MonitorThread
from gui.dashboard_tab import DashboardTab
from gui.log_panel import LogPanel
from gui.preview_capture import capture_current_preview
from gui.settings_tab import SettingsTab
from gui.startup_trace import mark_startup_phase, startup_trace_path, write_startup_trace
from gui.theme import TEXT
from runtime_output import console_print


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path, *, startup_started_at: float | None = None) -> None:
        mark_startup_phase("mainwindow_init_start")
        super().__init__()
        self.setWindowTitle("ToME - Scrying Mirror")
        self.resize(1280, 760)
        self.setMinimumSize(900, 560)
        self._startup_started_at = startup_started_at
        self._startup_timer_reported = False
        self._startup_metrics_path = config_path.parent / ".startup_timing.txt"

        # ── Log bridge: redirect stdout/stderr before anything prints ──
        self._log_bridge = LogBridge(self)
        self._log_bridge.install()
        mark_startup_phase("log_bridge_ready")

        # ── Input bridge ──
        self._input_bridge = InputBridge(self)
        self._input_bridge.input_needed.connect(self._handle_input_request)

        # ── Log panel (parented into dashboard's splitter) ──
        mark_startup_phase("log_panel_create_start")
        self._log_panel = LogPanel()
        mark_startup_phase("log_panel_create_done")

        # ── Top bar: status dots ─────────────────────────────────────────────
        mark_startup_phase("top_bar_create_start")
        top_bar = QWidget()
        top_bar_lay = QHBoxLayout(top_bar)
        top_bar_lay.setContentsMargins(8, 6, 8, 6)
        top_bar_lay.setSpacing(10)

        self._status_dot = QLabel("\u25cf Initializing")
        self._status_dot.setProperty("status", "warn")
        top_bar_lay.addStretch()
        top_bar_lay.addWidget(self._status_dot)

        self._game_dot = QLabel("\u25cf Game Inactive")
        self._game_dot.setProperty("status", "error")
        top_bar_lay.addWidget(self._game_dot)
        mark_startup_phase("top_bar_create_done")

        # ── Main content ──────────────────────────────────────────────────────
        central = QWidget()
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(top_bar)

        # ── Status bar ──
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Starting\u2026")
        mark_startup_phase("status_bar_ready")

        # ── Dashboard (log panel parented inside it) ──
        mark_startup_phase("dashboard_create_start")
        self._dashboard = DashboardTab(log_panel=self._log_panel)
        mark_startup_phase("dashboard_create_done")
        central_lay.addWidget(self._dashboard, 1)
        self.setCentralWidget(central)
        mark_startup_phase("dashboard_ready")

        self._char_menu = QMenu(self)
        self._actions_menu = QMenu(self)
        self._actions_menu.aboutToShow.connect(self._rebuild_actions_menu)
        self._dashboard._sheet_visual.set_character_menu(self._char_menu)
        self._dashboard._sheet_visual.set_actions_menu(self._actions_menu)

        self._settings_tab: SettingsTab | None = None
        self._char_items: list[tuple[str, str]] = []  # (folder_name, label)
        self._captured_preview_tabs: set[str] = set()

        # ── Wire signals ──
        self._log_bridge.message_ready.connect(self._log_panel.append)
        self._dashboard.analyze_requested.connect(self._run_analysis)
        self._dashboard.game_status_changed.connect(self._set_game_status)
        self._dashboard.game_connected.connect(self._report_startup_time)
        self._dashboard._subtabs.currentChanged.connect(self._maybe_capture_preview_from_tabs)
        self._dashboard._sheet_visual._content_tabs.currentChanged.connect(self._maybe_capture_preview_from_tabs)

        # ── Start monitor thread ──
        self._monitor = MonitorThread(config_path, self._input_bridge)
        mark_startup_phase("monitor_thread_start")
        self._monitor.start()
        mark_startup_phase("monitor_thread_started")

        # ── Poll until initialize_system finishes ──
        self._init_poll = QTimer(self)
        self._init_poll.setInterval(50)
        self._init_poll.timeout.connect(self._check_init)
        self._init_poll.start()
        QTimer.singleShot(0, self._check_init)

        self._watcher: QFileSystemWatcher | None = None
        mark_startup_phase("mainwindow_init_done")

    # ── Init polling ───────────────────────────────────────────────────────

    def _check_init(self) -> None:
        config = self._monitor.config
        if config is None:
            return

        mark_startup_phase("monitor_config_ready", characters=len(config.characters))
        self._init_poll.stop()
        self._set_monitor_status(active=True)
        self._dashboard.set_roots(config.character_sheets_root, config.backup_root)
        self.statusBar().showMessage("Monitor active")

        # Settings tab
        self._settings_tab = SettingsTab()
        self._settings_tab.load_config(config)
        self._settings_tab.config_saved.connect(self._on_config_saved)
        self._dashboard.set_settings_tab(self._settings_tab)

        # Populate character dropdown
        for char in config.characters:
            class_race, level = self._read_sheet_meta(config.character_sheets_root / f"data_{char.folder_name}.json")
            self._dashboard.add_character(char.folder_name, char.name)
            self._char_items.append(
                (
                    char.folder_name,
                    self._char_label(char.name, class_race, level),
                )
            )

        self._rebuild_character_menu()

        # Auto-select first character
        if self._char_items:
            self._select_character(self._char_items[0][0])

        # Watch for sheet updates
        config.character_sheets_root.mkdir(exist_ok=True)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.addPath(str(config.character_sheets_root))
        self._watcher.directoryChanged.connect(self._on_sheets_changed)
        mark_startup_phase("monitor_init_applied")

    # ── Status helpers ─────────────────────────────────────────────────────

    def _set_monitor_status(self, active: bool) -> None:
        if active:
            self._status_dot.setText("\u25cf Monitor Active")
            self._status_dot.setProperty("status", "ok")
        else:
            self._status_dot.setText("\u25cf Stopped")
            self._status_dot.setProperty("status", "error")
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)

    def _set_game_status(self, active: bool) -> None:
        if active:
            self._game_dot.setText("\u25cf Game Active")
            self._game_dot.setProperty("status", "ok")
        else:
            self._game_dot.setText("\u25cf Game Inactive")
            self._game_dot.setProperty("status", "error")
        self._game_dot.style().unpolish(self._game_dot)
        self._game_dot.style().polish(self._game_dot)

    def _maybe_capture_preview_from_tabs(self, _index: int) -> None:
        if self._dashboard._subtabs.currentWidget() is not self._dashboard._sheet_visual:
            return

        current_inner = self._dashboard._sheet_visual._content_tabs.currentIndex()
        preview_name = {
            0: "character-sheet-overview",
            1: "inventory-view",
        }.get(current_inner)
        if preview_name is None or preview_name in self._captured_preview_tabs:
            return

        try:
            capture_current_preview(self, preview_name)
        except RuntimeError as exc:
            print(f"[!] Preview capture skipped: {exc}")
            return

        self._captured_preview_tabs.add(preview_name)

    # ── Signal handlers ────────────────────────────────────────────────────

    def _select_character(self, folder_name: str) -> None:
        if folder_name:
            mark_startup_phase("character_select_start", folder_name=folder_name)
            self._dashboard.select_character(folder_name)
            mark_startup_phase("character_select_done", folder_name=folder_name)

    def _on_sheets_changed(self, _path: str) -> None:
        config = self._monitor.config
        if not config:
            return
        # Refresh character labels with updated level info
        updated_items: list[tuple[str, str]] = []
        for folder_name, _label in self._char_items:
            char = next((c for c in config.characters if c.folder_name == folder_name), None)
            if char:
                class_race, level = self._read_sheet_meta(
                    config.character_sheets_root / f"data_{char.folder_name}.json"
                )
                updated_items.append((folder_name, self._char_label(char.name, class_race, level)))
        self._char_items = updated_items
        self._rebuild_character_menu()
        self._dashboard.refresh_current()
        self.statusBar().showMessage("Character sheet updated", 4000)

    def _rebuild_character_menu(self) -> None:
        self._char_menu.clear()
        for folder_name, label in self._char_items:
            action = self._char_menu.addAction(label)
            action.triggered.connect(lambda checked=False, fn=folder_name: self._select_character(fn))

    def _rebuild_actions_menu(self) -> None:
        self._actions_menu.clear()
        folder_name = self._dashboard._current_folder
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
        header_lbl.setStyleSheet(f"font-weight: 700; color: {TEXT}; padding: 5px 12px 3px 12px;")
        header_action = QWidgetAction(self._actions_menu)
        header_action.setDefaultWidget(header_lbl)
        self._actions_menu.addAction(header_action)

        backups = self._get_backups(folder_name, config.backup_root)
        if backups:
            for backup_path in backups:
                label = "  " + self._format_backup_name(backup_path.name)
                action = self._actions_menu.addAction(label)
                action.triggered.connect(
                    lambda checked=False, fn=folder_name, bn=backup_path.name: self._restore_backup(fn, bn)
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

                has_transmo = self._dashboard._reader.read_has_transmo()
                schedule_scrying_sync(char, config, has_transmo=has_transmo)
                self.statusBar().showMessage(f"Sync scheduled for {char.name}", 3000)
                return

    def _on_config_saved(self, config: object) -> None:
        from models import AppConfig
        from monitor import save_config

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

    def _report_startup_time(self) -> None:
        if self._startup_timer_reported or self._startup_started_at is None:
            return
        elapsed = time.perf_counter() - self._startup_started_at
        console_print(f"[*] Startup time to game connection: {elapsed:.2f}s")
        if trace_path := startup_trace_path():
            console_print(f"[*] Startup trace: {trace_path}")
        try:
            self._startup_metrics_path.write_text(f"{elapsed:.6f}\n", encoding="utf-8")
        except OSError:
            pass
        write_startup_trace("game_connected_reported", elapsed_s=round(elapsed, 6))
        self.statusBar().showMessage(f"Connected to game in {elapsed:.2f}s", 5000)
        self._startup_timer_reported = True

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
        except IndexError, ValueError:
            return name

    @staticmethod
    def _read_sheet_meta(sheet_path: Path) -> tuple[str, str]:
        if not sheet_path.exists():
            return "", ""
        try:
            data = json.loads(sheet_path.read_text(encoding="utf-8"))
            ch = data.get("Character", {})
            cls = ch.get("Class", "")
            race = ch.get("Race", "")
            class_race = " / ".join(filter(None, [cls, race]))
            level = ch.get("Level / Exp", "").split(" ")[0]
            return class_race, level
        except OSError, json.JSONDecodeError, AttributeError:
            return "", ""

    # ── Cleanup ────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._init_poll.isActive():
            self._init_poll.stop()
        self._dashboard.shutdown()
        self._log_bridge.uninstall()
        super().closeEvent(event)
