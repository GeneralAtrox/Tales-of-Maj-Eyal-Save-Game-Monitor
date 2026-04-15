"""
enemy_panel.py
--------------
Collapsible enemy list widget for the dashboard.  Shows enemies from
game.level.entities, scanned on map change only.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.memory_reader import (
    DANGER_DEADLY,
    DANGER_DANGEROUS,
    DANGER_EASY,
    DANGER_MODERATE,
    DANGER_TRIVIAL,
    EntityInfo,
)
from gui.theme import (
    BORDER,
    GREEN,
    MAUVE,
    OVERLAY,
    RED,
    SUBTEXT0,
    SURFACE0,
    SURFACE2,
    TEXT,
    TEAL,
    YELLOW,
)

# ── NPC sprite lookup ────────────────────────────────────────────────────────

_ICON_DIR = Path(__file__).resolve().parent.parent / "Icons" / "npc"
_ICON_CACHE: dict[str, Path | None] = {}   # normalised name → path or None
_ICON_SIZE = 32


def _normalise(name: str) -> str:
    """'Skeleton Warrior' → 'skeleton_warrior'"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _find_icon(entity_name: str) -> Path | None:
    """
    Best-effort match of entity display name to an icon file.

    Tries exact match first, then strips leading unique-name prefix
    (e.g. "Jaedemas the Guardian" → "guardian"), then progressively
    shorter suffixes of the name.
    """
    key = _normalise(entity_name)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    candidates = [key]

    # Strip "Foo the <type>" prefix — use the part after "the"
    if "_the_" in key:
        candidates.append(key.split("_the_", 1)[1])

    # Try progressively shorter suffixes (e.g. "master_skeleton_archer")
    parts = key.split("_")
    for i in range(1, len(parts)):
        candidates.append("_".join(parts[i:]))

    for c in candidates:
        p = _ICON_DIR / f"{c}.png"
        if p.exists():
            _ICON_CACHE[key] = p
            return p

    _ICON_CACHE[key] = None
    return None

# Rank → display colour
_RANK_COLORS: dict[str, str] = {
    "Boss": RED,
    "Elite Boss": RED,
    "Unique": MAUVE,
    "Rare": YELLOW,
    "Elite": TEAL,
    "Normal": SUBTEXT0,
}

# Danger label → display colour
_DANGER_COLORS: dict[str, str] = {
    DANGER_TRIVIAL:   OVERLAY,
    DANGER_EASY:      GREEN,
    DANGER_MODERATE:  YELLOW,
    DANGER_DANGEROUS: "#fab387",   # Catppuccin peach / orange
    DANGER_DEADLY:    RED,
}


def _hp_color(pct: float) -> str:
    if pct > 0.5:
        return GREEN
    if pct > 0.25:
        return YELLOW
    return RED


class _EnemyCard(QFrame):
    """Single enemy row — sprite + name, HP bar, rank badge, key stats."""

    def __init__(self, entity: EntityInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EnemyCard")
        self.setStyleSheet(
            f"#EnemyCard {{"
            f"  background: {SURFACE0};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 4px;"
            f"}}"
        )

        # Outer horizontal layout: [sprite] [info column]
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 10, 6)
        outer.setSpacing(8)

        # ── Sprite ──
        icon_path = _find_icon(entity.name)
        if icon_path:
            pix = QPixmap(str(icon_path)).scaled(
                QSize(_ICON_SIZE, _ICON_SIZE),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl = QLabel()
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedSize(_ICON_SIZE, _ICON_SIZE)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            outer.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignTop)

        # ── Info column ──
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(3)

        # Row 1: name + rank badge + level + faction
        top = QHBoxLayout()
        top.setSpacing(8)

        rank_color = _RANK_COLORS.get(entity.rank_label, SUBTEXT0)

        name_lbl = QLabel(entity.name)
        bold = entity.rank_label in ("Unique", "Boss", "Elite Boss", "Rare")
        weight = "700" if bold else "400"
        name_lbl.setStyleSheet(f"font-weight: {weight}; font-size: 13px; color: {TEXT};")
        top.addWidget(name_lbl)

        rank_badge = QLabel(f" {entity.rank_label} ")
        rank_badge.setStyleSheet(
            f"background: {rank_color}; color: {SURFACE0}; font-size: 10px;"
            f" font-weight: 700; border-radius: 3px; padding: 1px 5px;"
        )
        top.addWidget(rank_badge)

        lvl_lbl = QLabel(f"Lv {entity.level:.0f}")
        lvl_lbl.setStyleSheet(f"color: {SUBTEXT0}; font-size: 11px;")
        top.addWidget(lvl_lbl)

        faction_lbl = QLabel(entity.faction)
        faction_lbl.setStyleSheet(f"color: {OVERLAY}; font-size: 11px;")
        top.addWidget(faction_lbl)

        top.addStretch()

        # Danger badge (right-aligned)
        danger_color = _DANGER_COLORS.get(entity.danger, OVERLAY)
        danger_badge = QLabel(f" {entity.danger} ")
        danger_badge.setStyleSheet(
            f"background: {danger_color}; color: {SURFACE0}; font-size: 10px;"
            f" font-weight: 700; border-radius: 3px; padding: 1px 6px;"
        )
        top.addWidget(danger_badge)

        info.addLayout(top)

        # Row 2: HP bar
        pct = entity.life / entity.max_life if entity.max_life > 0 else 0
        hp_color = _hp_color(pct)

        hp_row = QHBoxLayout()
        hp_row.setSpacing(6)

        hp_bar = QProgressBar()
        hp_bar.setRange(0, 1000)
        hp_bar.setValue(int(pct * 1000))
        hp_bar.setTextVisible(False)
        hp_bar.setFixedHeight(10)
        hp_bar.setStyleSheet(
            f"QProgressBar {{"
            f"  background: {SURFACE2}; border: none; border-radius: 4px;"
            f"}}"
            f"QProgressBar::chunk {{"
            f"  background: {hp_color}; border-radius: 4px;"
            f"}}"
        )
        hp_row.addWidget(hp_bar, stretch=1)

        hp_text = QLabel(f"{entity.life:.0f} / {entity.max_life:.0f}")
        hp_text.setStyleSheet(f"color: {hp_color}; font-size: 11px; font-weight: 600;")
        hp_text.setFixedWidth(100)
        hp_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hp_row.addWidget(hp_text)

        info.addLayout(hp_row)

        # Row 3: combat stats (compact)
        stats_parts: list[str] = []
        if entity.armor:
            stats_parts.append(f"Arm {entity.armor:.0f}")
        if entity.defense:
            stats_parts.append(f"Def {entity.defense:.0f}")
        if entity.phys_save:
            stats_parts.append(f"Phys {entity.phys_save:.0f}")
        if entity.spell_save:
            stats_parts.append(f"Spell {entity.spell_save:.0f}")
        if entity.mental_save:
            stats_parts.append(f"Mind {entity.mental_save:.0f}")

        if stats_parts:
            stats_lbl = QLabel("  |  ".join(stats_parts))
            stats_lbl.setStyleSheet(f"color: {OVERLAY}; font-size: 11px;")
            info.addWidget(stats_lbl)

        outer.addLayout(info, stretch=1)


class EnemyPanel(QWidget):
    """
    Scrollable enemy list panel.  Call ``update_enemies()`` with a fresh
    entity list whenever the map changes.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # ── Header ──
        hdr_row = QHBoxLayout()
        title = QLabel("Enemies")
        title.setProperty("heading", True)
        hdr_row.addWidget(title)

        self._map_label = QLabel("")
        self._map_label.setStyleSheet(f"color: {SUBTEXT0}; font-size: 11px;")
        hdr_row.addWidget(self._map_label)
        hdr_row.addStretch()

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {OVERLAY}; font-size: 11px;")
        hdr_row.addWidget(self._count_label)

        outer.addLayout(hdr_row)

        # ── Scrollable card list ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"background: transparent;")

        self._container = QWidget()
        self._card_layout = QVBoxLayout(self._container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(4)
        self._card_layout.addStretch()

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        self._empty_label = QLabel("No enemies detected")
        self._empty_label.setStyleSheet(f"color: {OVERLAY}; font-size: 12px;")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setVisible(True)
        outer.addWidget(self._empty_label)

    def set_map_name(self, level_id: str) -> None:
        self._map_label.setText(level_id)

    def update_enemies(self, enemies: list[EntityInfo]) -> None:
        """Replace the card list with fresh data."""
        # Clear old cards
        while self._card_layout.count() > 1:  # keep the stretch
            item = self._card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not enemies:
            self._empty_label.setVisible(True)
            self._count_label.setText("")
            return

        self._empty_label.setVisible(False)
        self._count_label.setText(f"{len(enemies)} enemies")

        for ent in enemies:
            card = _EnemyCard(ent)
            # Insert before the trailing stretch
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)
