from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyle,
    QStyleOptionSpinBox,
    QVBoxLayout,
    QWidget,
)

from game_data.boss_templates import BossTemplateStats, get_boss_templates, load_boss_template_stats
from gui.memory_reader import EntityInfo
from gui.theme import BG, BLUE, BORDER, GREEN, OVERLAY, RED, SUBTEXT0, SURFACE0, SURFACE1, SURFACE2, TEXT, YELLOW
from scoring.battle_simulator import (
    COMMON_DAMAGE_TYPES,
    BattleEnemySnapshot,
    BattleSimulatorState,
)
from scoring.enemy_threat import EnemyOffense, PlayerDefenses
from tome_practice import AutoPracticeResult, PracticeLaunchInfo, launch_manual_practice, run_auto_practice


def battle_enemy_from_entity(entity: EntityInfo) -> BattleEnemySnapshot:
    offense = EnemyOffense.from_all_fields(entity.all_fields, entity.name)
    offense.rank = entity.rank or offense.rank
    return BattleEnemySnapshot(
        name=entity.name,
        level=entity.level,
        life=entity.life,
        max_life=entity.max_life,
        rank_label=entity.rank_label,
        faction=entity.faction,
        type_name=entity.type_name,
        subtype=entity.subtype,
        offense=offense,
    )


def battle_enemy_from_boss_template(stats: BossTemplateStats) -> BattleEnemySnapshot:
    return BattleEnemySnapshot(
        name=stats.template.name,
        level=stats.level or 0.0,
        life=stats.max_life,
        max_life=stats.max_life,
        rank_label=stats.rank_name,
        faction=stats.faction,
        type_name=stats.type_name,
        subtype=stats.subtype,
        template_location=stats.template.location,
        template_level_label=stats.template.level_label,
        template_quest=stats.template.quest,
        template_warning=stats.warning,
        offense=EnemyOffense(
            name=stats.template.name,
            rank=stats.rank,
            global_speed=stats.global_speed,
            atk=stats.atk,
            dam=stats.dam,
            apr=stats.apr,
            crit_chance_pct=stats.crit_chance_pct,
            crit_power_bonus_pct=stats.crit_power_bonus_pct,
            physspeed=stats.physspeed,
            damage_type=stats.damage_type,
            inc_damage=dict(stats.inc_damage),
            resists_pen=dict(stats.resists_pen),
        ),
    )


class BattleSimulatorPanel(QWidget):
    _manual_launch_ready = Signal(object)
    _auto_result_ready = Signal(object)
    _practice_failed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = BattleSimulatorState()
        self._syncing_controls = False
        self._save_root: Path | None = None
        self._save_folder = ""
        self._save_char_name = ""
        self._practice_busy = False
        self._engine_status_text = "Practice and simulate launch exact bosses in a disposable cloned save."
        self._engine_summary_text = ""

        self._player_spins: dict[str, QDoubleSpinBox] = {}
        self._player_damage_spins: dict[str, dict[str, QDoubleSpinBox]] = {}
        self._enemy_line_edits: dict[str, QLineEdit] = {}
        self._enemy_scalar_spins: dict[str, QDoubleSpinBox] = {}
        self._enemy_offense_spins: dict[str, QDoubleSpinBox] = {}
        self._enemy_damage_type_combo = QComboBox()
        self._enemy_damage_spins: dict[str, dict[str, QDoubleSpinBox]] = {}
        self._result_values: dict[str, QLabel] = {}
        self._boss_templates = get_boss_templates()
        self._active_template_key = ""
        self._manual_launch_ready.connect(self._handle_manual_launch_ready)
        self._auto_result_ready.connect(self._handle_auto_result_ready)
        self._practice_failed.connect(self._handle_practice_failed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = self._section_frame()
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(12, 10, 12, 10)
        header_lay.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)
        title = QLabel("Battle Simulator")
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        self._enemy_title = QLabel("No enemy selected")
        self._enemy_title.setStyleSheet(f"font-size: 12px; color: {SUBTEXT0};")
        self._enemy_meta = QLabel("Select a monster in Enemies and add it to the simulator.")
        self._enemy_meta.setStyleSheet(f"font-size: 11px; color: {OVERLAY};")
        title_col.addWidget(title)
        title_col.addWidget(self._enemy_title)
        title_col.addWidget(self._enemy_meta)
        header_lay.addLayout(title_col)
        header_lay.addStretch(1)

        self._boss_template_combo = QComboBox()
        self._boss_template_combo.setMinimumContentsLength(36)
        self._boss_template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._boss_template_combo.addItem("Boss Template...")
        for template in self._boss_templates:
            self._boss_template_combo.addItem(template.display_label, template.key)
        self._boss_template_combo.activated.connect(self._on_boss_template_selected)
        header_lay.addWidget(self._boss_template_combo, 0, Qt.AlignmentFlag.AlignVCenter)

        self._practice_btn = QPushButton("Practice Fight In-Game")
        self._practice_btn.clicked.connect(self._on_practice_clicked)
        header_lay.addWidget(self._practice_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._auto_btn = QPushButton("Simulate Fight In-Game")
        self._auto_btn.clicked.connect(self._on_auto_clicked)
        header_lay.addWidget(self._auto_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._live_status = QLabel("Live player snapshot unavailable")
        self._live_status.setStyleSheet(f"font-size: 11px; color: {OVERLAY};")
        header_lay.addWidget(self._live_status, 0, Qt.AlignmentFlag.AlignVCenter)

        self._reset_player_btn = QPushButton("Reset Player To Live")
        self._reset_player_btn.clicked.connect(self._on_reset_player)
        header_lay.addWidget(self._reset_player_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._clear_enemy_btn = QPushButton("Clear Enemy")
        self._clear_enemy_btn.clicked.connect(self._on_clear_enemy)
        header_lay.addWidget(self._clear_enemy_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_player_editor())
        splitter.addWidget(self._build_enemy_editor())
        splitter.addWidget(self._build_results_panel())
        splitter.setSizes([520, 520, 320])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        root.addWidget(splitter, 1)

        self._refresh_ui()

    def set_save_context(self, save_root: Path | None, folder_name: str, char_name: str = "") -> None:
        resolved = save_root.expanduser().resolve() if save_root is not None else None
        if resolved == self._save_root and folder_name == self._save_folder and char_name == self._save_char_name:
            return
        self._save_root = resolved
        self._save_folder = folder_name
        self._save_char_name = char_name
        self._refresh_ui()

    def set_live_player(self, player: PlayerDefenses | None) -> None:
        self._state.set_live_player(player)
        self._refresh_ui()

    def load_enemy_snapshot(self, enemy: BattleEnemySnapshot) -> None:
        self._active_template_key = ""
        self._reset_engine_feedback()
        self._state.load_enemy(enemy)
        self._refresh_ui()

    def clear_enemy_snapshot(self) -> None:
        self._active_template_key = ""
        self._reset_engine_feedback()
        self._state.clear_enemy()
        self._refresh_ui()

    def _build_player_editor(self) -> QWidget:
        form_host = QWidget()
        root = QVBoxLayout(form_host)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        root.addWidget(self._section_title("Player Defenses"))
        root.addWidget(self._build_scalar_form(
            (
                ("max_life", "Max HP", 0.0, 100000.0),
                ("die_at", "Die At", -10000.0, 10000.0),
                ("armor", "Armor", 0.0, 5000.0),
                ("armor_hardiness_pct", "Armor Hardiness %", 0.0, 100.0),
                ("defense", "Defense", 0.0, 5000.0),
                ("evasion_pct", "Evasion %", 0.0, 100.0),
                ("ignore_direct_crits_pct", "Ignore Crits %", 0.0, 100.0),
            ),
            self._player_spins,
            self._on_player_scalar_changed,
        ))
        root.addWidget(
            self._build_damage_table(
                "Resists",
                "resists",
                self._player_damage_spins,
                self._on_player_damage_changed,
            )
        )
        root.addWidget(
            self._build_damage_table(
                "Resist Pen",
                "resists_pen",
                self._player_damage_spins,
                self._on_player_damage_changed,
            )
        )
        root.addWidget(
            self._build_damage_table(
                "Resist Cap Bonus",
                "resists_cap",
                self._player_damage_spins,
                self._on_player_damage_changed,
            )
        )
        root.addStretch(1)
        return self._wrap_scroll(form_host)

    def _build_enemy_editor(self) -> QWidget:
        form_host = QWidget()
        root = QVBoxLayout(form_host)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        root.addWidget(self._section_title("Enemy Snapshot"))
        root.addWidget(
            self._build_line_form(
                (
                    ("name", "Name"),
                    ("rank_label", "Rank Label"),
                    ("faction", "Faction"),
                    ("type_name", "Type"),
                    ("subtype", "Subtype"),
                ),
                self._enemy_line_edits,
                self._on_enemy_text_changed,
            )
        )
        root.addWidget(
            self._build_scalar_form(
                (
                    ("level", "Level", 0.0, 500.0),
                    ("life", "Life", 0.0, 1000000.0),
                    ("max_life", "Max Life", 0.0, 1000000.0),
                ),
                self._enemy_scalar_spins,
                self._on_enemy_scalar_changed,
            )
        )
        root.addWidget(
            self._build_scalar_form(
                (
                    ("rank", "Rank", 0.0, 10.0),
                    ("global_speed", "Global Speed", 0.0, 10.0),
                    ("dam", "Weapon Damage", 0.0, 10000.0),
                    ("atk", "Attack", 0.0, 5000.0),
                    ("apr", "Armor Pen", 0.0, 5000.0),
                    ("crit_chance_pct", "Crit %", 0.0, 100.0),
                    ("crit_power_bonus_pct", "Crit Power %", 0.0, 500.0),
                    ("physspeed", "Physical Speed", 0.0, 10.0),
                    ("talent_max_weapon_mult", "Talent Weapon Mult", 0.0, 20.0),
                ),
                self._enemy_offense_spins,
                self._on_enemy_offense_changed,
            )
        )
        root.addWidget(self._build_damage_type_selector())
        root.addWidget(
            self._build_damage_table(
                "Damage Bonus",
                "inc_damage",
                self._enemy_damage_spins,
                self._on_enemy_damage_changed,
            )
        )
        root.addWidget(
            self._build_damage_table(
                "Resist Pen",
                "resists_pen",
                self._enemy_damage_spins,
                self._on_enemy_damage_changed,
            )
        )
        root.addStretch(1)
        return self._wrap_scroll(form_host)

    def _build_results_panel(self) -> QWidget:
        panel = self._section_frame()
        root = QVBoxLayout(panel)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        root.addWidget(self._section_title("Result"))
        self._result_status = QLabel("")
        self._result_status.setWordWrap(True)
        self._result_status.setStyleSheet(f"font-size: 12px; color: {OVERLAY};")
        root.addWidget(self._result_status)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        rows = (
            ("tier", "Tier"),
            ("threat", "Threat"),
            ("expected", "Expected Damage"),
            ("raw", "Raw Damage"),
            ("hit_rate", "Hit Rate"),
            ("one_shot", "One-Shot"),
            ("worst_resist", "Damage Type"),
            ("best_bonus", "Damage Bonus"),
        )
        for row, (key, label) in enumerate(rows):
            key_lbl = QLabel(label)
            key_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            value_lbl = QLabel("--")
            value_lbl.setWordWrap(True)
            value_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT};")
            grid.addWidget(key_lbl, row, 0)
            grid.addWidget(value_lbl, row, 1)
            self._result_values[key] = value_lbl
        root.addLayout(grid)

        engine_title = QLabel("Engine Practice")
        engine_title.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;")
        root.addWidget(engine_title)

        self._engine_status = QLabel("")
        self._engine_status.setWordWrap(True)
        self._engine_status.setStyleSheet(f"font-size: 11px; color: {OVERLAY};")
        root.addWidget(self._engine_status)

        self._engine_summary = QPlainTextEdit()
        self._engine_summary.setReadOnly(True)
        self._engine_summary.setMinimumHeight(110)
        self._engine_summary.setStyleSheet(
            f"QPlainTextEdit {{ background: {SURFACE0}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; }"
        )
        root.addWidget(self._engine_summary)

        advice_title = QLabel("Advice")
        advice_title.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;")
        root.addWidget(advice_title)

        self._advice_box = QPlainTextEdit()
        self._advice_box.setReadOnly(True)
        self._advice_box.setMinimumHeight(220)
        self._advice_box.setStyleSheet(
            f"QPlainTextEdit {{ background: {SURFACE0}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px; }"
        )
        root.addWidget(self._advice_box, 1)
        return panel

    def _build_damage_type_selector(self) -> QWidget:
        frame = self._section_frame()
        layout = QFormLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self._enemy_damage_type_combo = QComboBox()
        for damage_type in COMMON_DAMAGE_TYPES:
            if damage_type != "all":
                self._enemy_damage_type_combo.addItem(damage_type)
        self._enemy_damage_type_combo.currentTextChanged.connect(self._on_enemy_damage_type_changed)
        layout.addRow(self._form_label("Weapon Damage Type"), self._enemy_damage_type_combo)
        return frame

    @staticmethod
    def _section_frame() -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {BG}; border: 1px solid {BORDER};")
        return frame

    @staticmethod
    def _section_title(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;")
        return lbl

    def _wrap_scroll(self, child: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: transparent;")
        scroll.setWidget(child)
        return scroll

    def _build_scalar_form(
        self,
        fields: tuple[tuple[str, str, float, float], ...],
        target: dict[str, QDoubleSpinBox],
        callback: Callable[[str, float], None],
    ) -> QWidget:
        frame = self._section_frame()
        layout = QFormLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        for key, label, min_value, max_value in fields:
            spin = self._new_spin(min_value, max_value)
            spin.valueChanged.connect(lambda value, field_name=key: callback(field_name, value))
            target[key] = spin
            layout.addRow(self._form_label(label), spin)
        return frame

    def _build_line_form(
        self,
        fields: tuple[tuple[str, str], ...],
        target: dict[str, QLineEdit],
        callback: Callable[[str, str], None],
    ) -> QWidget:
        frame = self._section_frame()
        layout = QFormLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        for key, label in fields:
            line = QLineEdit()
            line.textChanged.connect(lambda value, field_name=key: callback(field_name, value))
            target[key] = line
            layout.addRow(self._form_label(label), line)
        return frame

    def _build_damage_table(
        self,
        title: str,
        group: str,
        target: dict[str, dict[str, QDoubleSpinBox]],
        callback: Callable[[str, str, float], None],
    ) -> QWidget:
        frame = self._section_frame()
        root = QVBoxLayout(frame)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUBTEXT0};")
        root.addWidget(title_lbl)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        controls: dict[str, QDoubleSpinBox] = {}
        rows_per_col = (len(COMMON_DAMAGE_TYPES) + 1) // 2
        for index, damage_type in enumerate(COMMON_DAMAGE_TYPES):
            row = index % rows_per_col
            col = (index // rows_per_col) * 2
            label = QLabel(damage_type)
            label.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            spin = self._new_spin(-100.0, 500.0)
            spin.valueChanged.connect(
                lambda value, group_name=group, dtype=damage_type: callback(group_name, dtype, value)
            )
            controls[damage_type] = spin
            grid.addWidget(label, row, col)
            grid.addWidget(spin, row, col + 1)
        target[group] = controls
        root.addLayout(grid)
        return frame

    @staticmethod
    def _form_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
        return label

    @staticmethod
    def _new_spin(min_value: float, max_value: float) -> QDoubleSpinBox:
        spin = _NoWheelDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(min_value, max_value)
        spin.setSingleStep(1.0)
        spin.setKeyboardTracking(False)
        return spin

    def _on_reset_player(self) -> None:
        self._state.reset_player_to_live()
        self._refresh_ui()

    def _on_clear_enemy(self) -> None:
        self._active_template_key = ""
        self._state.clear_enemy()
        self._refresh_ui()

    def _on_boss_template_selected(self, index: int) -> None:
        template_key = self._boss_template_combo.itemData(index)
        if not isinstance(template_key, str) or not template_key:
            self._active_template_key = ""
            return
        stats = load_boss_template_stats(template_key)
        if stats is None:
            return
        self._active_template_key = template_key
        self._reset_engine_feedback()
        self._state.load_enemy(battle_enemy_from_boss_template(stats))
        self._refresh_ui()

    def _on_practice_clicked(self) -> None:
        self._start_practice_run("manual")

    def _on_auto_clicked(self) -> None:
        self._start_practice_run("auto")

    def _on_player_scalar_changed(self, field_name: str, value: float) -> None:
        if self._syncing_controls:
            return
        self._state.set_player_scalar(field_name, value)
        self._refresh_results()

    def _on_player_damage_changed(self, group: str, damage_type: str, value: float) -> None:
        if self._syncing_controls:
            return
        self._state.set_player_damage_value(group, damage_type, value)
        self._refresh_results()

    def _on_enemy_text_changed(self, field_name: str, value: str) -> None:
        if self._syncing_controls:
            return
        self._state.set_enemy_scalar(field_name, value)
        self._refresh_results()

    def _on_enemy_scalar_changed(self, field_name: str, value: float) -> None:
        if self._syncing_controls:
            return
        self._state.set_enemy_scalar(field_name, value)
        self._refresh_results()

    def _on_enemy_offense_changed(self, field_name: str, value: float) -> None:
        if self._syncing_controls:
            return
        self._state.set_enemy_offense_scalar(field_name, value)
        self._refresh_results()

    def _on_enemy_damage_type_changed(self, value: str) -> None:
        if self._syncing_controls:
            return
        self._state.set_enemy_offense_text("damage_type", value)
        self._refresh_results()

    def _on_enemy_damage_changed(self, group: str, damage_type: str, value: float) -> None:
        if self._syncing_controls:
            return
        self._state.set_enemy_damage_value(group, damage_type, value)
        self._refresh_results()

    def _refresh_ui(self) -> None:
        self._syncing_controls = True
        try:
            player = self._state.resolved_player()
            enemy = self._state.resolved_enemy()
            self._sync_boss_template_combo()
            self._sync_practice_controls()

            self._reset_player_btn.setEnabled(self._state.player_live is not None)
            if self._state.player_live is None:
                self._live_status.setText("Live player snapshot unavailable")
                self._live_status.setStyleSheet(f"font-size: 11px; color: {OVERLAY};")
            elif self._state.player_is_dirty:
                self._live_status.setText("Live player snapshot available; simulator is using local overrides")
                self._live_status.setStyleSheet(f"font-size: 11px; color: {YELLOW};")
            else:
                self._live_status.setText("Live player snapshot synced")
                self._live_status.setStyleSheet(f"font-size: 11px; color: {GREEN};")

            self._enemy_title.setText(enemy.name if enemy else "No enemy selected")
            if enemy:
                if enemy.template_location or enemy.template_quest:
                    parts = [
                        enemy.template_location,
                        enemy.template_level_label,
                        enemy.template_quest,
                        enemy.template_warning,
                    ]
                else:
                    parts = [
                        enemy.rank_label,
                        f"Lv {enemy.level:.0f}" if enemy.level else "",
                        enemy.faction,
                        " / ".join(part for part in (enemy.type_name, enemy.subtype) if part),
                    ]
                meta = "  ·  ".join(part for part in parts if part)
                self._enemy_meta.setText(meta or "Editing loaded enemy snapshot")
            else:
                self._enemy_meta.setText("Select a monster in Enemies and add it to the simulator.")

            self._set_spin_values(self._player_spins, player, (
                "max_life",
                "die_at",
                "armor",
                "armor_hardiness_pct",
                "defense",
                "evasion_pct",
                "ignore_direct_crits_pct",
            ))
            self._set_damage_values(self._player_damage_spins, player)

            self._set_line_values(
                self._enemy_line_edits,
                enemy,
                ("name", "rank_label", "faction", "type_name", "subtype"),
            )
            self._set_spin_values(self._enemy_scalar_spins, enemy, ("level", "life", "max_life"))
            self._set_enemy_offense_values(
                enemy,
                (
                    "rank",
                    "global_speed",
                    "dam",
                    "atk",
                    "apr",
                    "crit_chance_pct",
                    "crit_power_bonus_pct",
                    "physspeed",
                    "talent_max_weapon_mult",
                ),
            )
            self._set_enemy_damage_type(enemy)
            self._set_enemy_damage_values(enemy)
        finally:
            self._syncing_controls = False
        self._refresh_results()

    def _sync_boss_template_combo(self) -> None:
        target_index = 0
        if self._active_template_key:
            for index in range(1, self._boss_template_combo.count()):
                if self._boss_template_combo.itemData(index) == self._active_template_key:
                    target_index = index
                    break
        with QSignalBlocker(self._boss_template_combo):
            self._boss_template_combo.setCurrentIndex(target_index)

    def _refresh_results(self) -> None:
        result = self._state.compute()
        report = result.report
        self._engine_status.setText(self._engine_status_message())
        self._engine_summary.setPlainText(self._engine_summary_text)
        if report is None:
            self._result_status.setText(result.status)
            self._result_status.setStyleSheet(f"font-size: 12px; color: {OVERLAY};")
            for value_label in self._result_values.values():
                value_label.setText("--")
            self._advice_box.setPlainText("")
            return

        if result.enemy.template_warning and report.raw_damage <= 0 and report.expected_damage <= 0:
            self._result_status.setText(result.enemy.template_warning)
            self._result_status.setStyleSheet(f"font-size: 12px; color: {YELLOW};")
        else:
            self._result_status.setText(
                f"Simulating {result.enemy.name} against {result.player.effective_hp:.0f} effective HP."
            )
            self._result_status.setStyleSheet(f"font-size: 12px; color: {SUBTEXT0};")

        tier_color = RED if report.can_one_shot else (YELLOW if report.weapon_threat_pct >= 35 else GREEN)
        self._result_values["tier"].setText(
            f"<span style='color:{tier_color}; font-weight:700;'>{report.tier_label}</span>"
        )
        self._result_values["threat"].setText(f"{report.weapon_threat_pct:.1f}% of effective HP")
        self._result_values["expected"].setText(f"{report.expected_damage:.1f}")
        self._result_values["raw"].setText(f"{report.raw_damage:.1f}")
        self._result_values["hit_rate"].setText(f"{report.hit_rate_pct:.1f}%")
        self._result_values["one_shot"].setText("Yes" if report.can_one_shot else "No")
        self._result_values["worst_resist"].setText(
            f"{report.worst_resist_type}  (x{report.worst_resist_multiplier:.2f})"
        )
        self._result_values["best_bonus"].setText(f"{report.best_inc_type}  (+{report.best_inc_pct:.0f}%)")

        advice_lines = []
        for item in result.advice:
            suffix = "" if item.feasible else " [not enough alone]"
            advice_lines.append(f"- {item.description}{suffix}")
        if report.notes:
            advice_lines.extend(f"- {note}" for note in report.notes)
        self._advice_box.setPlainText("\n".join(advice_lines) if advice_lines else "No additional changes needed.")

    @staticmethod
    def _set_spin_values(
        controls: dict[str, QDoubleSpinBox],
        source: object | None,
        fields: tuple[str, ...],
    ) -> None:
        for field_name in fields:
            value = float(getattr(source, field_name, 0.0)) if source is not None else 0.0
            with QSignalBlocker(controls[field_name]):
                controls[field_name].setValue(value)

    @staticmethod
    def _set_line_values(
        controls: dict[str, QLineEdit],
        source: object | None,
        fields: tuple[str, ...],
    ) -> None:
        for field_name in fields:
            value = str(getattr(source, field_name, "")) if source is not None else ""
            with QSignalBlocker(controls[field_name]):
                controls[field_name].setText(value)

    def _set_damage_values(self, controls: dict[str, dict[str, QDoubleSpinBox]], player: PlayerDefenses | None) -> None:
        if player is None:
            source_groups = {"resists": {}, "resists_pen": {}, "resists_cap": {}}
        else:
            source_groups = {
                "resists": player.resists,
                "resists_pen": player.resists_pen,
                "resists_cap": player.resists_cap,
            }
        for group_name, group_controls in controls.items():
            source = source_groups[group_name]
            for damage_type, spin in group_controls.items():
                with QSignalBlocker(spin):
                    spin.setValue(float(source.get(damage_type, 0.0)))

    def _set_enemy_offense_values(self, enemy: BattleEnemySnapshot | None, fields: tuple[str, ...]) -> None:
        offense = enemy.offense if enemy is not None else None
        for field_name in fields:
            value = float(getattr(offense, field_name, 0.0)) if offense is not None else 0.0
            with QSignalBlocker(self._enemy_offense_spins[field_name]):
                self._enemy_offense_spins[field_name].setValue(value)

    def _set_enemy_damage_type(self, enemy: BattleEnemySnapshot | None) -> None:
        damage_type = enemy.offense.damage_type if enemy is not None else "PHYSICAL"
        index = self._enemy_damage_type_combo.findText(damage_type)
        if index < 0:
            index = self._enemy_damage_type_combo.findText("PHYSICAL")
        with QSignalBlocker(self._enemy_damage_type_combo):
            self._enemy_damage_type_combo.setCurrentIndex(max(0, index))

    def _set_enemy_damage_values(self, enemy: BattleEnemySnapshot | None) -> None:
        if enemy is None:
            source_groups = {"inc_damage": {}, "resists_pen": {}}
        else:
            source_groups = {
                "inc_damage": enemy.offense.inc_damage,
                "resists_pen": enemy.offense.resists_pen,
            }
        for group_name, group_controls in self._enemy_damage_spins.items():
            source = source_groups[group_name]
            for damage_type, spin in group_controls.items():
                with QSignalBlocker(spin):
                    spin.setValue(float(source.get(damage_type, 0.0)))

    def _sync_practice_controls(self) -> None:
        enabled = bool(self._save_root and self._save_folder and self._active_template_key and not self._practice_busy)
        self._practice_btn.setEnabled(enabled)
        self._auto_btn.setEnabled(enabled)

    def _start_practice_run(self, mode: str) -> None:
        if self._practice_busy:
            return
        if self._save_root is None or not self._save_folder:
            self._engine_status_text = "Practice launch requires a tracked character save."
            self._engine_summary_text = ""
            self._refresh_results()
            return
        if not self._active_template_key:
            self._engine_status_text = "Practice launch requires a boss template from the dropdown."
            self._engine_summary_text = ""
            self._refresh_results()
            return

        action = "Launching practice fight..." if mode == "manual" else "Running AI simulation..."
        self._practice_busy = True
        self._engine_status_text = action
        self._engine_summary_text = (
            "Engine practice uses the exact boss definition from the game files.\n"
            "The editable quick-threat fields are not applied to these runs."
        )
        self._refresh_ui()

        save_root = self._save_root
        folder_name = self._save_folder
        template_key = self._active_template_key
        worker = threading.Thread(
            target=self._practice_worker,
            args=(mode, save_root, folder_name, template_key),
            daemon=True,
        )
        worker.start()

    def _practice_worker(self, mode: str, save_root: Path, folder_name: str, template_key: str) -> None:
        try:
            if mode == "manual":
                launch = launch_manual_practice(
                    save_root=save_root,
                    folder_name=folder_name,
                    template_key=template_key,
                )
                self._manual_launch_ready.emit(launch)
            else:
                result = run_auto_practice(
                    save_root=save_root,
                    folder_name=folder_name,
                    template_key=template_key,
                )
                self._auto_result_ready.emit(result)
        except Exception as exc:  # noqa: BLE001
            self._practice_failed.emit(str(exc))

    def _handle_manual_launch_ready(self, launch: PracticeLaunchInfo) -> None:
        self._practice_busy = False
        self._engine_status_text = "Practice fight launched in a disposable cloned save."
        monitor_note = ""
        if launch.used_shared_launcher:
            monitor_note = (
                "\nUsing the main t-engine.exe launcher. The live monitor may follow the practice instance "
                "while it is open."
            )
        self._engine_summary_text = (
            f"Template: {launch.template_label}\n"
            f"Clone save: {launch.clone_name}\n"
            f"Clone path: {launch.clone_path}{monitor_note}"
        )
        self._refresh_ui()

    def _handle_auto_result_ready(self, result: AutoPracticeResult) -> None:
        self._practice_busy = False
        self._engine_status_text = result.status or "Simulation finished."
        lines = [
            f"Template: {result.launch.template_label}",
            f"Winner: {result.winner or 'unknown'}",
            f"Turns: {result.turns}",
        ]
        incoming_hits = [
            event
            for event in result.damage_events
            if event.target_role == "player" and event.amount > 0
        ]
        if incoming_hits:
            max_hit = max(incoming_hits, key=lambda event: event.amount)
            lines.append(f"Engine max incoming hit: {max_hit.amount:.1f} from {max_hit.source}")
            quick_report = self._state.compute().report
            if quick_report is not None and max_hit.amount > 0:
                ratio = quick_report.expected_damage / max_hit.amount
                lines.append(f"Quick estimate: {quick_report.expected_damage:.1f} ({ratio:.2f}x engine max)")
        elif result.damage_events:
            lines.append(f"Damage events: {len(result.damage_events)} recorded")
        if result.reason:
            lines.append(f"Reason: {result.reason}")
        if result.detail:
            lines.append(f"Detail: {result.detail}")
        lines.append(f"Clone save: {result.launch.clone_name}")
        if result.launch.used_shared_launcher:
            lines.append("Launcher: shared t-engine.exe (live monitor may attach to the practice instance)")
        self._engine_summary_text = "\n".join(lines)
        self._refresh_ui()

    def _handle_practice_failed(self, message: str) -> None:
        self._practice_busy = False
        self._engine_status_text = "Practice launch failed."
        self._engine_summary_text = message
        self._refresh_ui()

    def _reset_engine_feedback(self) -> None:
        self._engine_status_text = "Practice and simulate launch exact bosses in a disposable cloned save."
        self._engine_summary_text = ""

    def _engine_status_message(self) -> str:
        if self._practice_busy:
            return self._engine_status_text
        if not self._active_template_key:
            return "Select a boss template to enable engine-backed practice and AI simulation."
        if self._save_root is None or not self._save_folder:
            return "Select a tracked character save to enable engine-backed practice."
        if self._engine_summary_text:
            return self._engine_status_text
        return (
            "Engine-backed practice uses the exact boss definition from the game files in a disposable cloned save.\n"
            "The editable quick-threat fields stay in this panel only."
        )


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Ignore wheel input so scrolling the editor does not mutate values."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(28)
        self.setStyleSheet(
            "QDoubleSpinBox { padding-right: 24px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 20px; }"
        )
        self._normalize_font_point_size()
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            line_edit.setFont(self.font())

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            step = self._step_from_pos(event.position().toPoint())
            if step != 0:
                self.stepBy(step)
                event.accept()
                return
        super().mousePressEvent(event)

    def _normalize_font_point_size(self) -> None:
        font = self.font()
        if font.pointSizeF() > 0:
            return
        pixel_size = font.pixelSize()
        if pixel_size > 0:
            # Approximate Qt's default 96 DPI conversion so the widget has a
            # real point size even when the app stylesheet specifies pixels.
            font.setPointSizeF(max(1.0, pixel_size * 72.0 / 96.0))
        else:
            font.setPointSize(10)
        self.setFont(font)

    def _step_from_pos(self, pos) -> int:
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        style = self.style()
        up_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxUp,
            self,
        )
        if up_rect.contains(pos):
            return 1
        down_rect = style.subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxDown,
            self,
        )
        if down_rect.contains(pos):
            return -1
        return 0
