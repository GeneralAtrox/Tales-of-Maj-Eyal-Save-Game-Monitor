from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from game_data.npc_db import get_npc_db
from game_data.talent_db import get_talent_db
from gui.enemy_panel import EnemyPanel, player_stats_to_defenses
from gui.memory_reader import MemoryReader, is_process_running, take_preattached_reader
from gui.sheet_view import CharacterSheetView
from gui.theme import BORDER, SURFACE0

_MONO_FONT = '"Cascadia Code", "Consolas", "Courier New", monospace'


class DashboardTab(QWidget):
    """Main monitor workspace: character sub-tabs | enemies | output log."""

    analyze_requested = Signal(str, str)  # folder_name, question
    game_status_changed = Signal(bool)  # True = active
    game_connected = Signal()  # emitted after a successful game attach
    _attach_succeeded = Signal()
    _enemies_ready = Signal(list)  # list[EntityInfo] — bg thread → main thread
    _live_inventory_ready = Signal(object, object, object, object, object, object, object)

    def __init__(
        self,
        log_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sheets_root: Path | None = None
        self._current_folder: str | None = None
        self._chars: dict[str, str] = {}  # folder_name → display name
        self._settings_tab: QWidget | None = None

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
        self._inventory_poll_pending = False
        self._cache_warm_started = False
        self._shutting_down = False

        self._preattach_poll = QTimer(self)
        self._preattach_poll.setInterval(100)
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

        # ── 2-column splitter ──────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        # Left — character sub-tabs
        self._subtabs = QTabWidget()
        self._sheet_visual = CharacterSheetView()
        self._subtabs.addTab(self._sheet_visual, "Character Sheet")
        self._subtabs.addTab(self._build_sheet_tab(), "Sheet")
        self._subtabs.addTab(self._build_analysis_tab(), "Analysis")
        splitter.addWidget(self._subtabs)

        self._enemy_panel = EnemyPanel()
        self._sheet_visual.set_enemy_panel(self._enemy_panel)
        self._attach_succeeded.connect(self._on_attach_succeeded)
        self._enemies_ready.connect(self._handle_enemies_ready)
        self._live_inventory_ready.connect(self._handle_live_inventory_ready)
        splitter.addWidget(log_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([1180, 560])
        root.addWidget(splitter)

        # ── Finalize the memory reader now that _sheet_visual exists ──────
        # Either (a) pre-attach already located _G → defer the post-attach
        # flow via the event loop so it fires after __init__ fully returns
        # and MainWindow has had a chance to register characters, or (b) no
        # hot reader → fall through to the normal process-detection path.
        QTimer.singleShot(0, self._start_background_warmup)
        QTimer.singleShot(0, self._check_preattached_reader)
        QTimer.singleShot(0, self._check_game_process)

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

    # ── Public API ─────────────────────────────────────────────────────────

    def set_roots(self, sheets_root: Path, backups_root: Path) -> None:  # noqa: ARG002
        self._sheets_root = sheets_root

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
        self._reader.detach()

    def set_analysis_result(self, text: str) -> None:
        self._analysis_output.setPlainText(text)
        self._analyze_btn.setEnabled(True)
        self._analyze_btn.setText("Analyze with Claude")

    def set_settings_tab(self, tab: QWidget) -> None:
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
        char_name = self._chars.get(folder_name, "")
        if not self._game_session_ready:
            self._sheet_view.setPlainText("Connecting to game...\n\nCharacter data will load after a live attach.")
            self._sheet_visual.set_game_connected(False)
            self._sheet_visual.load({}, char_name)
            return
        sheet_path = self._sheets_root / f"data_{folder_name}.json"
        if sheet_path.exists():
            try:
                data = json.loads(sheet_path.read_text(encoding="utf-8"))
                self._sheet_view.setPlainText(json.dumps(data, indent=2))
                self._sheet_visual.set_game_connected(True)
                self._sheet_visual.load(data, char_name, defer_reload=self._inventory_poll_pending)
                return
            except (OSError, json.JSONDecodeError) as exc:
                self._sheet_view.setPlainText(f"Error reading sheet:\n{exc}")
                self._sheet_visual.set_game_connected(True)
                self._sheet_visual.load({}, char_name)
                return
        placeholder = (
            "No character sheet available yet.\n\nSave in-game to trigger a sync, or use Actions \u2192 Force Sync."
        )
        self._sheet_view.setPlainText(placeholder)
        self._sheet_visual.set_game_connected(True)
        self._sheet_visual.load({}, char_name)

    # ── Memory reader polling ──────────────────────────────────────────────

    def _start_background_warmup(self) -> None:
        if self._cache_warm_started:
            return
        self._cache_warm_started = True
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
            self._check_game_process()
            return

        self._awaiting_preattach = False
        self._preattach_poll.stop()
        self._reader = reader
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
            threading.Thread(target=self._attach_reader, daemon=True).start()
        elif not active and self._reader.attached:
            self._reader.detach()
            self._game_session_ready = False
            self._enemy_panel.set_loading(False)
            self._sheet_visual.clear_hp()
            self._sheet_visual.clear_sprite()
            self._sheet_visual.clear_live_inventory()
            self._sheet_visual.set_game_connected(False)
            if self._current_folder:
                self._load_sheet(self._current_folder)

    def _attach_reader(self) -> None:
        try:
            ok = self._reader.attach()
        finally:
            self._attach_pending = False
        if ok and not self._shutting_down:
            self._attach_succeeded.emit()

    def _on_attach_succeeded(self) -> None:
        """Post-attach finalization — runs from either the bg attach thread or
        the main thread when adopting a pre-warmed reader."""
        if self._shutting_down:
            return
        self._game_session_ready = True
        self._sheet_visual.set_game_connected(True)
        self.game_connected.emit()
        QTimer.singleShot(0, self._refresh_after_attach)

    def _refresh_after_attach(self) -> None:
        if self._shutting_down or not self._reader.attached:
            return
        self.refresh_current()
        self._poll_hp()
        self._poll_level_id()
        self._poll_progression()
        self._poll_inventory()

    def _poll_hp(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
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
                self._enemy_panel.set_loading(False)
                self._sheet_visual.clear_hp()  # also clears mana via clear_hp
                self._sheet_visual.clear_exp()
                self._sheet_visual.clear_sprite()
                self._sheet_visual.clear_live_inventory()
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
            self._enemy_panel.set_map_name(level_id)
            self._enemy_panel.set_loading(True)
            threading.Thread(target=self._scan_entities, daemon=True).start()

    def _scan_entities(self) -> None:
        entities = self._reader.read_entities(min_rank=1.5)
        self._enemies_ready.emit(entities)

    def _handle_enemies_ready(self, entities: list) -> None:
        defenses = None
        if self._reader.attached:
            try:
                stats = self._reader.read_player_stats()
            except Exception:
                stats = None
            defenses = player_stats_to_defenses(stats)
        self._enemy_panel.update_enemies(entities, defenses)

    def _poll_progression(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
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
            self._sheet_visual.clear_live_inventory()
            self._sheet_visual.set_live_talents(None)
            return
        if self._inventory_poll_pending:
            return
        self._inventory_poll_pending = True
        threading.Thread(target=self._read_live_inventory_bundle, daemon=True).start()

    def _poll_talents(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            self._sheet_visual.set_live_talents(None)
            return
        try:
            talents = self._reader.read_player_talents()
        except KeyboardInterrupt:
            self._handle_forced_interrupt()
            return
        self._sheet_visual.set_live_talents(talents)

    def _read_live_inventory_bundle(self) -> None:
        try:
            equipment, current, transmog = self._reader.read_player_inventory()
            talents = self._reader.read_player_talents()
            sustains = self._reader.read_sustain_talents()
            effects = self._reader.read_player_effects()
            prodigies = self._reader.read_prodigies()
        except Exception:  # noqa: BLE001
            equipment, current, transmog, talents, sustains, effects, prodigies = [], [], [], None, None, None, None
        if self._shutting_down:
            return
        self._live_inventory_ready.emit(equipment, current, transmog, talents, sustains, effects, prodigies)

    def _handle_live_inventory_ready(self, equipment, current, transmog, talents, sustains, effects, prodigies) -> None:
        self._inventory_poll_pending = False
        if not self._reader.attached:
            return
        self._sheet_visual.set_live_bundle(
            equipment,
            current,
            transmog,
            talents,
            sustains,
            effects,
            prodigies if prodigies else None,
        )

    def _poll_prodigies(self) -> None:
        if self._shutting_down:
            return
        if not self._reader.attached:
            self._sheet_visual.set_live_prodigies(None)
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
        question = self._analysis_input.toPlainText().strip()
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.setText("Analyzing…")
        self.analyze_requested.emit(self._current_folder, question)
