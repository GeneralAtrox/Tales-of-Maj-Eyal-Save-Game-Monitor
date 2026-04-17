from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
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
from gui.enemy_panel import EnemyPanel
from gui.memory_reader import MemoryReader
from gui.sheet_view import CharacterSheetView
from gui.theme import BORDER, SURFACE0

_MONO_FONT = '"Cascadia Code", "Consolas", "Courier New", monospace'


class DashboardTab(QWidget):
    """Main monitor workspace: character sub-tabs | enemies | output log."""

    analyze_requested    = Signal(str, str)  # folder_name, question
    game_status_changed  = Signal(bool)      # True = active
    game_connected       = Signal()          # emitted after a successful game attach
    _enemies_ready       = Signal(list)      # list[EntityInfo] — bg thread → main thread
    _live_inventory_ready = Signal(object, object, object, object)

    def __init__(
        self,
        log_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sheets_root: Path | None  = None
        self._current_folder: str | None = None
        self._chars: dict[str, str] = {}   # folder_name → display name
        self._settings_tab: QWidget | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Warm NPC database in background (zip parse / cache load) ──────
        threading.Thread(target=get_npc_db, daemon=True).start()
        threading.Thread(target=get_talent_db, daemon=True).start()

        # ── Memory reader ──────────────────────────────────────────────────
        self._reader = MemoryReader()
        self._attach_pending = False
        self._hp_fail_count  = 0
        self._last_level_id: str | None = None
        self._game_session_ready = False
        self._inventory_poll_pending = False

        self._game_poll = QTimer(self)
        self._game_poll.setInterval(3000)
        self._game_poll.timeout.connect(self._check_game_process)
        self._game_poll.start()
        self._check_game_process()

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
        self._subtabs.addTab(self._sheet_visual,         "Character Sheet")
        self._subtabs.addTab(self._build_sheet_tab(),    "Sheet")
        self._subtabs.addTab(self._build_analysis_tab(), "Analysis")
        splitter.addWidget(self._subtabs)

        self._enemy_panel = EnemyPanel()
        self._sheet_visual.set_enemy_panel(self._enemy_panel)
        self._enemies_ready.connect(self._handle_enemies_ready)
        self._live_inventory_ready.connect(self._handle_live_inventory_ready)
        splitter.addWidget(log_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([1180, 560])
        root.addWidget(splitter)

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
            "Ask a question about this character…\n"
            'e.g. "What should I fix before entering Dreadfell?"'
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
        self._analysis_output.setPlaceholderText(
            "Analysis results will appear here after you click Analyze."
        )

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
        char_name  = self._chars.get(folder_name, "")
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
                self._sheet_visual.load(data, char_name)
                return
            except (OSError, json.JSONDecodeError) as exc:
                self._sheet_view.setPlainText(f"Error reading sheet:\n{exc}")
                self._sheet_visual.set_game_connected(True)
                self._sheet_visual.load({}, char_name)
                return
        placeholder = (
            "No character sheet available yet.\n\n"
            "Save in-game to trigger a sync, or use Actions \u2192 Force Sync."
        )
        self._sheet_view.setPlainText(placeholder)
        self._sheet_visual.set_game_connected(True)
        self._sheet_visual.load({}, char_name)

    # ── Memory reader polling ──────────────────────────────────────────────

    def _check_game_process(self) -> None:
        try:
            result = subprocess.run(
                ["tasklist"], capture_output=True, text=True, timeout=2,
            )
            active = "t-engine" in result.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            active = False
        self.game_status_changed.emit(active)

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
        if ok:
            self._game_session_ready = True
            self._sheet_visual.set_game_connected(True)
            self.refresh_current()
            self._poll_progression()
            self._poll_talents()
            self.game_connected.emit()

    def _poll_hp(self) -> None:
        if not self._reader.attached:
            return
        hp = self._reader.read_player_hp()
        if hp is not None:
            self._hp_fail_count = 0
            life, max_life = hp
            self._sheet_visual.set_hp(life, max_life)
            sprite = self._reader.read_player_sprite()
            if sprite is not None:
                self._sheet_visual.set_sprite(*sprite)
            else:
                self._sheet_visual.clear_sprite()
            mana = self._reader.read_player_mana()
            if mana is not None:
                self._sheet_visual.set_mana(*mana)
            else:
                self._sheet_visual.clear_mana()
            exp = self._reader.read_player_exp()
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
                self._sheet_visual.clear_hp()   # also clears mana via clear_hp
                self._sheet_visual.clear_exp()
                self._sheet_visual.clear_sprite()
                self._sheet_visual.clear_live_inventory()
                self._sheet_visual.set_game_connected(False)
                if self._current_folder:
                    self._load_sheet(self._current_folder)
                self._hp_fail_count = 0

    def _poll_level_id(self) -> None:
        if not self._reader.attached:
            return
        level_id = self._reader.read_level_id()
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
        self._enemy_panel.update_enemies(entities)

    def _poll_progression(self) -> None:
        if not self._reader.attached:
            return
        visited = self._reader.read_visited_zones()
        deaths  = self._reader.read_unique_deaths()
        current = self._reader.read_current_zone()
        self._sheet_visual.update_progression(visited, deaths, current)

    def _poll_inventory(self) -> None:
        if not self._reader.attached:
            self._sheet_visual.clear_live_inventory()
            self._sheet_visual.set_live_talents(None)
            return
        if self._inventory_poll_pending:
            return
        self._inventory_poll_pending = True
        threading.Thread(target=self._read_live_inventory_bundle, daemon=True).start()

    def _poll_talents(self) -> None:
        if not self._reader.attached:
            self._sheet_visual.set_live_talents(None)
            return
        self._sheet_visual.set_live_talents(self._reader.read_player_talents())

    def _read_live_inventory_bundle(self) -> None:
        try:
            equipment, current, transmog = self._reader.read_player_inventory()
            talents = self._reader.read_player_talents()
        except Exception:  # noqa: BLE001
            equipment, current, transmog, talents = [], [], [], None
        self._live_inventory_ready.emit(equipment, current, transmog, talents)

    def _handle_live_inventory_ready(self, equipment, current, transmog, talents) -> None:
        self._inventory_poll_pending = False
        if not self._reader.attached:
            return
        self._sheet_visual.set_live_inventory(equipment, current, transmog)
        self._sheet_visual.set_live_talents(talents)

    def _poll_prodigies(self) -> None:
        if not self._reader.attached:
            self._sheet_visual.set_live_prodigies(None)
            return
        available = self._reader.read_prodigies()
        self._sheet_visual.set_live_prodigies(available if available else None)

    # ── Analysis ───────────────────────────────────────────────────────────

    def _on_analyze_clicked(self) -> None:
        if not self._current_folder:
            return
        question = self._analysis_input.toPlainText().strip()
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.setText("Analyzing…")
        self.analyze_requested.emit(self._current_folder, question)
