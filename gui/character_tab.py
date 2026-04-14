from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.sheet_view import CharacterSheetView
from gui.theme import BORDER, SUBTEXT0, SURFACE0, SURFACE1, TEXT

_MONO_FONT = "\"Cascadia Code\", \"Consolas\", \"Courier New\", monospace"


class CharacterTab(QWidget):
    """Per-character detail view: Sheet JSON, Backups list, Analysis prompt."""

    analyze_requested  = Signal(str, str)  # folder_name, question
    restore_requested  = Signal(str, str)  # folder_name, backup_name

    def __init__(
        self,
        sheets_root: Path,
        backups_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sheets_root  = sheets_root
        self._backups_root = backups_root
        self._current_folder: str | None = None
        self._chars: dict[str, str] = {}  # folder_name → display name

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # ── Character selector ──
        selector_row = QHBoxLayout()
        lbl = QLabel("Character:")
        lbl.setProperty("subheading", True)
        self._combo = QComboBox()
        self._combo.setMinimumWidth(240)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        selector_row.addWidget(lbl)
        selector_row.addWidget(self._combo)
        selector_row.addStretch()
        root.addLayout(selector_row)

        # ── Sub-tabs ──
        self._subtabs = QTabWidget()
        root.addWidget(self._subtabs)

        # Sub-tab indices (keep in sync with SUBTAB_* constants below)
        self._sheet_visual = CharacterSheetView()
        self._subtabs.addTab(self._sheet_visual,          "Character Sheet")
        self._subtabs.addTab(self._build_sheet_tab(),     "Sheet")
        self._subtabs.addTab(self._build_backups_tab(),   "Backups")
        self._subtabs.addTab(self._build_analysis_tab(),  "Analysis")

    SUBTAB_CHARACTER_SHEET = 0
    SUBTAB_RAW_SHEET       = 1
    SUBTAB_BACKUPS         = 2
    SUBTAB_ANALYSIS        = 3

    # ── Tab builders ─────────────────────────────────────────────────────

    def _build_sheet_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
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

    def _build_backups_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        self._backups_list = QListWidget()
        self._backups_list.currentRowChanged.connect(self._on_backup_selection_changed)
        lay.addWidget(self._backups_list)

        btn_row = QHBoxLayout()
        self._restore_btn = QPushButton("Restore Selected Save")
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore_clicked)
        btn_row.addWidget(self._restore_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        return w

    def _build_analysis_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        q_label = QLabel("Question:")
        q_label.setProperty("subheading", True)
        self._analysis_input = QPlainTextEdit()
        self._analysis_input.setPlaceholderText(
            "Ask a question about this character…\n"
            "e.g. \"What should I fix before entering Dreadfell?\""
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
        self._analysis_output.setStyleSheet(f"font-size: 13px;")
        self._analysis_output.setPlaceholderText(
            "Analysis results will appear here after you click Analyze."
        )

        for widget in (q_label, self._analysis_input):
            lay.addWidget(widget)
        lay.addLayout(btn_row)
        lay.addWidget(a_label)
        lay.addWidget(self._analysis_output)
        return w

    # ── Public API ────────────────────────────────────────────────────────

    def add_character(self, folder_name: str, name: str) -> None:
        if folder_name not in self._chars:
            self._chars[folder_name] = name
            self._combo.addItem(name, userData=folder_name)

    def select_character(self, folder_name: str) -> None:
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == folder_name:
                self._combo.setCurrentIndex(i)
                return

    def select_subtab(self, index: int) -> None:
        self._subtabs.setCurrentIndex(index)

    def refresh_current(self) -> None:
        if self._current_folder:
            self._load_sheet(self._current_folder)
            self._load_backups(self._current_folder)

    def set_analysis_result(self, text: str) -> None:
        self._analysis_output.setPlainText(text)
        self._analyze_btn.setEnabled(True)
        self._analyze_btn.setText("Analyze with Claude")

    # ── Internals ─────────────────────────────────────────────────────────

    def _on_combo_changed(self, index: int) -> None:
        folder_name = self._combo.itemData(index)
        if folder_name:
            self._current_folder = folder_name
            self._load_sheet(folder_name)
            self._load_backups(folder_name)

    def _load_sheet(self, folder_name: str) -> None:
        sheet_path = self._sheets_root / f"data_{folder_name}.json"
        char_name  = self._chars.get(folder_name, "")
        if sheet_path.exists():
            try:
                data = json.loads(sheet_path.read_text(encoding="utf-8"))
                self._sheet_view.setPlainText(json.dumps(data, indent=2))
                self._sheet_visual.load(data, char_name)
            except (OSError, json.JSONDecodeError) as exc:
                self._sheet_view.setPlainText(f"Error reading sheet:\n{exc}")
        else:
            placeholder = (
                "No character sheet available yet.\n\n"
                "Save in-game to trigger a sync, or use Actions → Force Sync\n"
                "from the Monitor tab."
            )
            self._sheet_view.setPlainText(placeholder)
            self._sheet_visual.load({}, char_name)

    def _load_backups(self, folder_name: str) -> None:
        self._backups_list.clear()
        self._restore_btn.setEnabled(False)
        backup_dir = self._backups_root / folder_name
        if backup_dir.exists():
            backups = sorted(
                (p for p in backup_dir.iterdir() if p.is_dir()),
                reverse=True,
            )
            for b in backups:
                item = QListWidgetItem(b.name)
                item.setToolTip(str(b))
                self._backups_list.addItem(item)
        if self._backups_list.count() == 0:
            self._backups_list.addItem("No backups yet")

    def _on_backup_selection_changed(self, row: int) -> None:
        item = self._backups_list.item(row)
        has_valid = item is not None and item.text() != "No backups yet"
        self._restore_btn.setEnabled(has_valid)

    def _on_restore_clicked(self) -> None:
        if not self._current_folder:
            return
        item = self._backups_list.currentItem()
        if item and item.text() != "No backups yet":
            self.restore_requested.emit(self._current_folder, item.text())

    def _on_analyze_clicked(self) -> None:
        if not self._current_folder:
            return
        question = self._analysis_input.toPlainText().strip()
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.setText("Analyzing…")
        self.analyze_requested.emit(self._current_folder, question)
