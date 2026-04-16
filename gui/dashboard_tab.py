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
from gui.enemy_panel import EnemyPanel
from gui.memory_reader import MemoryReader
from gui.sheet_view import CharacterSheetView
from gui.theme import BORDER, SURFACE0

_MONO_FONT = '"Cascadia Code", "Consolas", "Courier New", monospace'


class DashboardTab(QWidget):
    """Main monitor workspace: character sub-tabs | enemies | output log."""

    analyze_requested    = Signal(str, str)  # folder_name, question
    game_status_changed  = Signal(bool)      # True = active
    _enemies_ready       = Signal(list)      # list[EntityInfo] — bg thread → main thread

    def __init__(
        self,
        log_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sheets_root: Path | None  = None
        self._current_folder: str | None = None
        self._chars: dict[str, str] = {}   # folder_name → display name

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Warm NPC database in background (zip parse / cache load) ──────
        threading.Thread(target=get_npc_db, daemon=True).start()

        # ── Memory reader ──────────────────────────────────────────────────
        self._reader = MemoryReader()
        self._attach_pending = False
        self._hp_fail_count  = 0
        self._last_level_id: str | None = None

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

        # ── 3-column splitter ──────────────────────────────────────────────
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

        # Centre — enemy panel
        right_stack = QSplitter(Qt.Orientation.Vertical)
        right_stack.setHandleWidth(1)
        right_stack.setChildrenCollapsible(False)

        self._enemy_panel = EnemyPanel()
        self._enemies_ready.connect(self._handle_enemies_ready)
        right_stack.addWidget(self._enemy_panel)

        # Bottom of right stack — output log (owned by MainWindow, parented here)
        right_stack.addWidget(log_panel)
        right_stack.setStretchFactor(0, 3)
        right_stack.setStretchFactor(1, 2)
        right_stack.setSizes([520, 260])
        splitter.addWidget(right_stack)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([760, 980])
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

    # ── Sheet loading ──────────────────────────────────────────────────────

    def _load_sheet(self, folder_name: str) -> None:
        if not self._sheets_root:
            return
        sheet_path = self._sheets_root / f"data_{folder_name}.json"
        char_name  = self._chars.get(folder_name, "")
        if sheet_path.exists():
            try:
                data = json.loads(sheet_path.read_text(encoding="utf-8"))
                self._sheet_view.setPlainText(json.dumps(data, indent=2))
                self._sheet_visual.load(data, char_name)
                return
            except (OSError, json.JSONDecodeError) as exc:
                self._sheet_view.setPlainText(f"Error reading sheet:\n{exc}")
                self._sheet_visual.load({}, char_name)
                return
        placeholder = (
            "No character sheet available yet.\n\n"
            "Save in-game to trigger a sync, or use Actions \u2192 Force Sync."
        )
        self._sheet_view.setPlainText(placeholder)
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
            self._enemy_panel.set_loading(False)
            self._sheet_visual.clear_hp()
            self._sheet_visual.clear_sprite()

    def _attach_reader(self) -> None:
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
        else:
            self._hp_fail_count += 1
            if self._hp_fail_count >= 5 and not self._attach_pending:
                self._reader.detach()
                self._enemy_panel.set_loading(False)
                self._sheet_visual.clear_hp()   # also clears mana via clear_hp
                self._sheet_visual.clear_sprite()
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

    # ── Analysis ───────────────────────────────────────────────────────────

    def _on_analyze_clicked(self) -> None:
        if not self._current_folder:
            return
        question = self._analysis_input.toPlainText().strip()
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.setText("Analyzing…")
        self.analyze_requested.emit(self._current_folder, question)
