from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from game_data.npc_db import get_npc_db
from game_data.talent_db import get_talent_db
from gui.memory_reader import MemoryReader, is_process_running, take_preattached_reader
from gui.sheet_view import CharacterSheetView
from gui.startup_trace import mark_startup_phase, write_startup_trace
from gui.theme import BORDER, SURFACE0

_MONO_FONT = '"Cascadia Code", "Consolas", "Courier New", monospace'


class DashboardTab(QWidget):
    """Main monitor workspace: character subtabs and live game integrations."""

    analyze_requested = Signal(str, str)  # folder_name, question
    game_status_changed = Signal(bool)  # True = active
    game_connected = Signal()  # emitted after a successful game attach
    sheet_content_tab_changed = Signal(int)
    _attach_succeeded = Signal()
    _enemies_ready = Signal(list)  # list[EntityInfo] — bg thread → main thread
    _live_inventory_ready = Signal(object, object, object, object, object, object, object, object)

    def __init__(
        self,
        log_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        mark_startup_phase("dashboard_init_start")
        super().__init__(parent)
        self._sheets_root: Path | None = None
        self._save_root: Path | None = None
        self._current_folder: str | None = None
        self._chars: dict[str, str] = {}  # folder_name → display name
        self._settings_tab: QWidget | None = None
        self._character_menu: object | None = None
        self._actions_menu: object | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Memory reader (state only; finalization is deferred below) ────
        # App startup kicks off a background pre-attach.  We do not block the
        # UI waiting for it here; instead we poll briefly and adopt the reader
        # once it finishes, falling back to a local attach only if needed.
        self._reader = MemoryReader()
        self._attach_pending = False
        self._awaiting_preattach = True
        self._preattach_deadline = time.monotonic() + 12.0
        self._hp_fail_count = 0
        self._last_level_id: str | None = None
        self._game_session_ready = False
        self._sheet_loaded_for_session = False
        self._inventory_poll_pending = False
        self._cache_warm_started = False
        self._shutting_down = False
        self._latest_entities: list = []
        self._latest_defenses: Any | None = None
        self._enemy_panel: QWidget | None = None
        self._sheet_visual: CharacterSheetView | None = None
        self._sheet_tab_index = -1
        self._secondary_tabs_ready = False
        self._pending_log_panel = log_panel
        self._sheet_view: QPlainTextEdit | None = None
        self._analysis_input: QPlainTextEdit | None = None
        self._analysis_output: QTextEdit | None = None
        self._analyze_btn: QPushButton | None = None

        self._preattach_poll = QTimer(self)
        self._preattach_poll.setInterval(25)
        self._preattach_poll.timeout.connect(self._check_preattached_reader)
        self._preattach_poll.start()

        self._game_poll = QTimer(self)
        self._game_poll.setInterval(1000)
        self._game_poll.timeout.connect(self._check_game_process)
        self._game_poll.start()

        self._hp_poll = QTimer(self)
        self._hp_poll.setInterval(1000)
        self._hp_poll.timeout.connect(self._poll_hp)
        self._hp_poll.start()

        self._level_poll = QTimer(self)
        self._level_poll.setInterval(2000)
        self._level_poll.timeout.connect(self._poll_level_id)
        self._level_poll.start()

        self._progression_poll = QTimer(self)
        self._progression_poll.setInterval(5000)
        self._progression_poll.timeout.connect(self._poll_progression)
        self._progression_poll.start()

        self._inventory_poll = QTimer(self)
        self._inventory_poll.setInterval(2500)
        self._inventory_poll.timeout.connect(self._poll_inventory)
        self._inventory_poll.start()

        self._prodigy_poll = QTimer(self)
        self._prodigy_poll.setInterval(5000)
        self._prodigy_poll.timeout.connect(self._poll_prodigies)
        self._prodigy_poll.start()
        mark_startup_phase("dashboard_timers_ready")

        self._subtabs = QTabWidget()
        mark_startup_phase("sheet_tabs_deferred")
        root.addWidget(self._subtabs, 1)

        mark_startup_phase("enemy_panel_deferred")
        self._attach_succeeded.connect(self._on_attach_succeeded)
        self._enemies_ready.connect(self._handle_enemies_ready)
        self._live_inventory_ready.connect(self._handle_live_inventory_ready)

        # ── Finalize the memory reader via the event loop ─────────────────
        # Keep the first UI pass light enough for a hot pre-attach to report
        # before the full sheet widgets are constructed.
        QTimer.singleShot(0, self._start_background_warmup)
        QTimer.singleShot(0, self._check_preattached_reader)
        QTimer.singleShot(0, self._check_game_process)
        mark_startup_phase("dashboard_init_done")

    # ── Sub-tab builders ───────────────────────────────────────────────────

    def _build_sheet_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        self._sheet_view = QPlainTextEdit()
        self._sheet_view.setReadOnly(True)
        self._sheet_view.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  font-family: {_MONO_FONT};"
            f"  font-size: 12px;"
            f"  background: {SURFACE0};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 4px;"
            f"}}"
        )
        lay.addWidget(self._sheet_view)
        return w

    def _build_analysis_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(8)

        q_label = QLabel("Question:")
        q_label.setProperty("subheading", True)
        self._analysis_input = QPlainTextEdit()
        self._analysis_input.setPlaceholderText(
            'Ask a question about this character…\ne.g. "What should I fix before entering Dreadfell?"'
        )
        self._analysis_input.setFixedHeight(80)

        btn_row = QHBoxLayout()
        self._analyze_btn = QPushButton("Analyze with Claude")
        self._analyze_btn.setProperty("accent", True)
        self._analyze_btn.clicked.connect(self._on_analyze_clicked)
        btn_row.addWidget(self._analyze_btn)
        btn_row.addStretch()

        a_label = QLabel("Analysis:")
        a_label.setProperty("subheading", True)
        self._analysis_output = QTextEdit()
        self._analysis_output.setReadOnly(True)
        self._analysis_output.setStyleSheet("font-size: 13px;")
        self._analysis_output.setPlaceholderText("Analysis results will appear here after you click Analyze.")

        lay.addWidget(q_label)
        lay.addWidget(self._analysis_input)
        lay.addLayout(btn_row)
        lay.addWidget(a_label)
        lay.addWidget(self._analysis_output)
        return w

    def _ensure_main_tabs(self) -> None:
        self._ensure_sheet_visual()
        self._ensure_secondary_tabs()

    def _ensure_sheet_visual(self) -> CharacterSheetView:
        if self._sheet_visual is not None:
            return self._sheet_visual

        mark_startup_phase("sheet_visual_create_start")
        sheet_visual = CharacterSheetView()
        mark_startup_phase("sheet_visual_create_done")
        if self._pending_log_panel.objectName() != "LogPlaceholder":
            mark_startup_phase("sheet_log_panel_attach_start")
            sheet_visual.set_log_panel(self._pending_log_panel)
            mark_startup_phase("sheet_log_panel_attach_done")
        if self._character_menu is not None:
            sheet_visual.set_character_menu(self._character_menu)
        if self._actions_menu is not None:
            sheet_visual.set_actions_menu(self._actions_menu)

        self._sheet_visual = sheet_visual
        mark_startup_phase("sheet_visual_tab_add_start")
        self._sheet_tab_index = self._subtabs.insertTab(0, sheet_visual, "Character Sheet")
        mark_startup_phase("sheet_visual_tab_add_done")
        sheet_visual._content_tabs.currentChanged.connect(self._on_sheet_content_tab_changed)
        return sheet_visual

    def _ensure_secondary_tabs(self) -> None:
        if self._secondary_tabs_ready:
            return
        mark_startup_phase("sheet_plain_tab_create_start")
        self._subtabs.addTab(self._build_sheet_tab(), "Sheet")
        mark_startup_phase("sheet_plain_tab_create_done")
        mark_startup_phase("analysis_tab_create_start")
        self._subtabs.addTab(self._build_analysis_tab(), "Analysis")
        mark_startup_phase("analysis_tab_create_done")
        self._secondary_tabs_ready = True

    def _ensure_enemy_panel(self) -> Any:
        if self._enemy_panel is not None:
            return self._enemy_panel

        mark_startup_phase("enemy_panel_create_start")
        from gui.enemy_panel import EnemyPanel

        panel = EnemyPanel()
        mark_startup_phase("enemy_panel_create_done")
        self._enemy_panel = panel
        sheet_visual = self._ensure_sheet_visual()
        sheet_visual.set_enemy_panel(panel)
        panel.simulate_requested.connect(sheet_visual.load_battle_enemy)
        if self._last_level_id is not None:
            panel.set_map_name(self._last_level_id)
        if self._latest_entities:
            panel.update_enemies(self._latest_entities, self._latest_defenses)
        return panel

    @staticmethod
    def _player_stats_to_defenses(stats: Any) -> Any:
        from gui.enemy_panel import player_stats_to_defenses

        return player_stats_to_defenses(stats)

    def _on_sheet_content_tab_changed(self, index: int) -> None:
        if self._sheet_visual is not None and index == self._sheet_visual._enemy_tab_index:
            self._ensure_enemy_panel()
        self.sheet_content_tab_changed.emit(index)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_character_menu(self, menu: object) -> None:
        self._character_menu = menu
        if self._sheet_visual is not None:
            self._sheet_visual.set_character_menu(menu)

    def set_actions_menu(self, menu: object) -> None:
        self._actions_menu = menu
        if self._sheet_visual is not None:
            self._sheet_visual.set_actions_menu(menu)

    def finalize_hot_preattach(self) -> None:
        """Adopt a completed pre-attach immediately after MainWindow signal wiring."""
        if self._shutting_down or not self._awaiting_preattach:
            return
        mark_startup_phase("preattach_sync_check_start")
        self._check_preattached_reader()
        mark_startup_phase(
            "preattach_sync_check_done",
            awaiting=self._awaiting_preattach,
            attached=self._reader.attached,
        )

    def set_roots(self, sheets_root: Path, backups_root: Path) -> None:  # noqa: ARG002
        self._sheets_root = sheets_root
        self._save_root = sheets_root.parent

    def add_character(self, folder_name: str, name: str) -> None:
        self._chars[folder_name] = name

    def select_character(self, folder_name: str) -> None:
        """Load sheet for folder_name if it changed."""
        if folder_name and folder_name != self._current_folder:
            self._current_folder = folder_name
            self._load_sheet(folder_name)

    def refresh_current(self) -> None:
        if self._current_folder:
            self._load_sheet(self._current_folder)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        for timer in (
            self._preattach_poll,
            self._game_poll,
            self._hp_poll,
            self._level_poll,
            self._progression_poll,
            self._inventory_poll,
            self._prodigy_poll,
        ):
            if timer.isActive():
                timer.stop()
        self._attach_pending = False
        self._awaiting_preattach = False
        self._inventory_poll_pending = False
        self._sheet_loaded_for_session = False
        self._reader.detach()

    def set_analysis_result(self, text: str) -> None:
        self._ensure_secondary_tabs()
        assert self._analysis_output is not None
        assert self._analyze_btn is not None
        self._analysis_output.setPlainText(text)
        self._analyze_btn.setEnabled(True)
        self._analyze_btn.setText("Analyze with Claude")

    def set_settings_tab(self, tab: QWidget) -> None:
        self._ensure_main_tabs()
        if self._settings_tab is tab:
            return
        if self._settings_tab is not None:
            index = self._subtabs.indexOf(self._settings_tab)
            if index >= 0:
                self._subtabs.removeTab(index)
        self._settings_tab = tab
        self._subtabs.addTab(tab, "Settings")

    # ── Sheet loading ──────────────────────────────────────────────────────

    def _load_sheet(self, folder_name: str) -> None:
        if not self._sheets_root:
            return
        self._ensure_main_tabs()
        assert self._sheet_visual is not None
        assert self._sheet_view is not None
        mark_startup_phase("sheet_load_start", folder_name=folder_name, game_ready=self._game_session_ready)
        char_name = self._chars.get(folder_name, "")
        self._sheet_visual.set_save_context(self._save_root, folder_name, char_name)
        if not self._game_session_ready:
            self._sheet_loaded_for_session = False
            self._sheet_view.setPlainText("Connecting to game...\n\nCharacter data will load after a live attach.")
            self._sheet_visual.set_game_connected(False)
            self._sheet_visual.load({}, char_name)
            mark_startup_phase("sheet_load_waiting_for_game", folder_name=folder_name)
            return
        sheet_path = self._sheets_root / f"data_{folder_name}.json"
        if sheet_path.exists():
            try:
                data = json.loads(sheet_path.read_text(encoding="utf-8"))
                mark_startup_phase("sheet_json_read_done", folder_name=folder_name)
                self._sheet_view.setPlainText(json.dumps(data, indent=2))
                self._sheet_visual.set_game_connected(True)
                self._sheet_visual.load(data, char_name, defer_reload=self._inventory_poll_pending)
                self._sheet_loaded_for_session = True
                mark_startup_phase("sheet_visual_load_done", folder_name=folder_name)
                return
            except (OSError, json.JSONDecodeError) as exc:
                self._sheet_view.setPlainText(f"Error reading sheet:\n{exc}")
                self._sheet_visual.set_game_connected(True)
                self._sheet_visual.load({}, char_name)
                self._sheet_loaded_for_session = True
                mark_startup_phase("sheet_load_failed", folder_name=folder_name, error=exc)
                return
        placeholder = (
            "No character sheet available yet.\n\nSave in-game to trigger a sync, or use Actions \u2192 Force Sync."
        )
        self._sheet_view.setPlainText(placeholder)
        self._sheet_visual.set_game_connected(True)
        self._sheet_visual.load({}, char_name)
        self._sheet_loaded_for_session = True
        mark_startup_phase("sheet_load_placeholder", folder_name=folder_name)

    # ── Memory reader polling ──────────────────────────────────────────────

    def _start_background_warmup(self) -> None:
        if self._cache_warm_started:
            return
        self._cache_warm_started = True
        mark_startup_phase("static_cache_warmup_started")
        threading.Thread(target=get_npc_db, daemon=True).start()
        threading.Thread(target=get_talent_db, daemon=True).start()

    def _check_preattached_reader(self) -> None:
        if self._shutting_down:
            return
        if not self._awaiting_preattach:
            return

        try:
            reader = take_preattached_reader(wait_timeout=0.0)
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        if reader is None:
            if time.monotonic() < self._preattach_deadline:
                return
            self._awaiting_preattach = False
            self._preattach_poll.stop()
            mark_startup_phase("preattach_timeout")
            self._check_game_process()
            return

        self._awaiting_preattach = False
        self._preattach_poll.stop()
        self._reader = reader
        mark_startup_phase("preattach_reader_adopted", attached=self._reader.attached)
        if self._reader.attached:
            self.game_status_changed.emit(True)
            self._attach_succeeded.emit()
        else:
            self._check_game_process()

    def _check_game_process(self) -> None:
        if self._shutting_down:
            return
        try:
            active = is_process_running("t-engine.exe")
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        self.game_status_changed.emit(active)

        if self._awaiting_preattach:
            return

        if active and not self._reader.attached and not self._attach_pending:
            self._attach_pending = True
            mark_startup_phase("attach_thread_start")
            threading.Thread(target=self._attach_reader, daemon=True).start()
        elif not active and self._reader.attached:
            self._reader.detach()
            self._game_session_ready = False
            self._sheet_loaded_for_session = False
            if self._enemy_panel is not None:
                self._enemy_panel.set_loading(False)
            if self._sheet_visual is not None:
                self._sheet_visual.clear_hp()
                self._sheet_visual.clear_sprite()
                self._sheet_visual.clear_live_inventory()
                self._sheet_visual.set_live_player_defenses(None)
                self._sheet_visual.set_game_connected(False)
            if self._current_folder:
                self._load_sheet(self._current_folder)

    def _attach_reader(self) -> None:
        mark_startup_phase("reader_attach_start")
        ok = False
        try:
            ok = self._reader.attach()
        finally:
            self._attach_pending = False
        mark_startup_phase("reader_attach_done", ok=ok)
        if ok and not self._shutting_down:
            self._attach_succeeded.emit()

    def _on_attach_succeeded(self) -> None:
        """Post-attach finalization — runs from either the bg attach thread or
        the main thread when adopting a pre-warmed reader."""
        if self._shutting_down:
            return
        self._game_session_ready = True
        self._sheet_loaded_for_session = False
        if self._sheet_visual is not None:
            self._sheet_visual.set_game_connected(True)
        mark_startup_phase("attach_succeeded")
        self.game_connected.emit()
        QTimer.singleShot(0, self._refresh_after_attach)

    def _refresh_after_attach(self) -> None:
        if self._shutting_down or not self._reader.attached:
            return
        self._ensure_main_tabs()
        assert self._sheet_visual is not None
        self._sheet_visual.set_game_connected(True)
        mark_startup_phase("refresh_after_attach_start")
        if self._current_folder and not self._sheet_loaded_for_session:
            self.refresh_current()
        self._poll_hp()
        self._poll_level_id()
        self._poll_progression()
        self._poll_inventory()
        mark_startup_phase("refresh_after_attach_dispatched")

    def _poll_hp(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            return
        if self._sheet_visual is None:
            return
        try:
            hp = self._reader.read_player_hp()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        if hp is not None:
            self._hp_fail_count = 0
            life, max_life = hp
            self._sheet_visual.set_hp(life, max_life)
            try:
                sprite = self._reader.read_player_sprite()
                mana = self._reader.read_player_mana()
                exp = self._reader.read_player_exp()
            except KeyboardInterrupt:
                self._handle_forced_interrupt()
                return
            if sprite is not None:
                self._sheet_visual.set_sprite(*sprite)
            else:
                self._sheet_visual.clear_sprite()
            if mana is not None:
                self._sheet_visual.set_mana(*mana)
            else:
                self._sheet_visual.clear_mana()
            if exp is not None:
                self._sheet_visual.set_exp(*exp)
            else:
                self._sheet_visual.clear_exp()
        else:
            self._hp_fail_count += 1
            if self._hp_fail_count >= 5 and not self._attach_pending:
                self._reader.detach()
                self._game_session_ready = False
                self._sheet_loaded_for_session = False
                if self._enemy_panel is not None:
                    self._enemy_panel.set_loading(False)
                if self._sheet_visual is not None:
                    self._sheet_visual.clear_hp()  # also clears mana via clear_hp
                    self._sheet_visual.clear_exp()
                    self._sheet_visual.clear_sprite()
                    self._sheet_visual.clear_live_inventory()
                    self._sheet_visual.set_live_player_defenses(None)
                    self._sheet_visual.set_game_connected(False)
                if self._current_folder:
                    self._load_sheet(self._current_folder)
                self._hp_fail_count = 0

    def _poll_level_id(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            return
        try:
            level_id = self._reader.read_level_id()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        if level_id is None:
            return
        if level_id != self._last_level_id:
            self._last_level_id = level_id
            if self._enemy_panel is not None:
                self._enemy_panel.set_map_name(level_id)
                self._enemy_panel.set_loading(True)
            threading.Thread(target=self._scan_entities, daemon=True).start()

    def _scan_entities(self) -> None:
        mark_startup_phase("entities_scan_start")
        entities = self._reader.read_entities(min_rank=1.5)
        mark_startup_phase("entities_scan_done", count=len(entities))
        self._enemies_ready.emit(entities)

    def _handle_enemies_ready(self, entities: list) -> None:
        self._ensure_main_tabs()
        assert self._sheet_visual is not None
        self._latest_entities = entities
        defenses = None
        if self._reader.attached:
            try:
                stats = self._reader.read_player_stats()
            except Exception:
                stats = None
            defenses = self._player_stats_to_defenses(stats)
        self._latest_defenses = defenses
        self._sheet_visual.set_live_player_defenses(defenses)
        self._ensure_enemy_panel().update_enemies(entities, defenses)
        write_startup_trace("entities_ready_applied", count=len(entities))

    def _poll_progression(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            return
        if self._sheet_visual is None:
            return
        try:
            visited = self._reader.read_visited_zones()
            deaths = self._reader.read_unique_deaths()
            uniques = self._reader.read_unique_encounters()
            current = self._reader.read_current_zone()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        self._sheet_visual.update_progression(visited, deaths, uniques, current)

    def _poll_inventory(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            if self._sheet_visual is not None:
                self._sheet_visual.clear_live_inventory()
                self._sheet_visual.set_live_talents(None)
                self._sheet_visual.set_live_player_defenses(None)
            return
        if self._inventory_poll_pending:
            return
        self._inventory_poll_pending = True
        mark_startup_phase("live_inventory_thread_start")
        threading.Thread(target=self._read_live_inventory_bundle, daemon=True).start()

    def _poll_talents(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            if self._sheet_visual is not None:
                self._sheet_visual.set_live_talents(None)
            return
        if self._sheet_visual is None:
            return
        try:
            talents = self._reader.read_player_talents()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        self._sheet_visual.set_live_talents(talents)

    def _read_live_inventory_bundle(self) -> None:
        mark_startup_phase("live_inventory_read_start")
        try:
            equipment, current, transmog = self._reader.read_player_inventory()
            talents = self._reader.read_player_talents()
            sustains = self._reader.read_sustain_talents()
            effects = self._reader.read_player_effects()
            prodigies = self._reader.read_prodigies()
            player_stats = self._reader.read_player_stats()
        except Exception:  # noqa: BLE001
            equipment = []
            current = []
            transmog = []
            talents = None
            sustains = None
            effects = None
            prodigies = None
            player_stats = None
        mark_startup_phase(
            "live_inventory_read_done",
            equipment=len(equipment),
            current=len(current),
            transmog=len(transmog),
            talents=0 if talents is None else len(talents),
        )
        if self._shutting_down:
            return
        self._live_inventory_ready.emit(
            equipment,
            current,
            transmog,
            talents,
            sustains,
            effects,
            prodigies,
            player_stats,
        )

    def _handle_live_inventory_ready(
        self,
        equipment,
        current,
        transmog,
        talents,
        sustains,
        effects,
        prodigies,
        player_stats,
    ) -> None:
        self._inventory_poll_pending = False
        if not self._reader.attached:
            return
        self._ensure_main_tabs()
        assert self._sheet_visual is not None
        self._sheet_visual.set_live_bundle(
            equipment,
            current,
            transmog,
            talents,
            sustains,
            effects,
            prodigies if prodigies else None,
        )
        defenses = self._player_stats_to_defenses(player_stats)
        self._latest_defenses = defenses
        self._sheet_visual.set_live_player_defenses(defenses)
        if self._latest_entities:
            self._ensure_enemy_panel().update_enemies(self._latest_entities, defenses)
        write_startup_trace(
            "live_inventory_applied",
            equipment=len(equipment),
            current=len(current),
            transmog=len(transmog),
            talents=0 if talents is None else len(talents),
        )

    def _poll_prodigies(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            if self._sheet_visual is not None:
                self._sheet_visual.set_live_prodigies(None)
            return
        if self._sheet_visual is None:
            return
        try:
            available = self._reader.read_prodigies()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        self._sheet_visual.set_live_prodigies(available if available else None)

    def _handle_forced_interrupt(self) -> None:
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ── Analysis ───────────────────────────────────────────────────────────

    def _on_analyze_clicked(self) -> None:
        if not self._current_folder:
            return
        if self._analysis_input is None or self._analyze_btn is None:
            return
        question = self._analysis_input.toPlainText().strip()
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.setText("Analyzing…")
        self.analyze_requested.emit(self._current_folder, question)
