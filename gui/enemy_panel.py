"""
enemy_panel.py
--------------
Collapsible enemy list widget for the dashboard.  Shows enemies from
game.level.entities, scanned on map change only.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game_data.npc_db import NpcRecord, get_npc_db
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

_ICONS_ROOT = Path(__file__).resolve().parent.parent / "Icons"
_ICON_CACHE: dict[str, Path | None] = {}   # image_hint key → Path or None
_ICON_SIZE = 32

# Truncate lore text longer than this in the card (full text goes to tooltip).
_DESC_MAX_CHARS = 160


def _resolve_icon(image_hint: str) -> Path | None:
    """
    Resolve a confirmed image path (e.g. ``"npc/troll_f.png"`` or
    ``"player/runic_golem/base_02.png"``) to a local file under Icons/.

    Only called with ground-truth paths from live memory or the NPC db —
    no fuzzy matching.  Returns None if the file is not present locally.
    """
    if not image_hint:
        return None
    if image_hint in _ICON_CACHE:
        return _ICON_CACHE[image_hint]
    # Full path match: Icons/<category>/<name>.png  (preserves subdirs)
    p = _ICONS_ROOT / image_hint
    if p.exists():
        _ICON_CACHE[image_hint] = p
        return p
    # Stem-only fallback under Icons/npc/ (handles bare filenames)
    stem = Path(image_hint).stem
    p2 = _ICONS_ROOT / "npc" / f"{stem}.png"
    result = p2 if p2.exists() else None
    _ICON_CACHE[image_hint] = result
    return result

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
    """Single enemy row — sprite + name, HP bar, rank badge, key stats, lore."""

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

        # Look up NPC record for confirmed image path and lore text.
        npc: NpcRecord | None = get_npc_db().get(entity.name.lower())

        # Image source priority — only confirmed ground-truth paths used,
        # no fuzzy guessing:
        #   1. entity.image  — read live from the entity's Lua table in memory
        #                      (exact for direct-image entities; empty for
        #                      resolvers.nice_tile, which stores "invis.png")
        #   2. npc.image     — parsed from game files (correct for nice_tile
        #                      entities where memory gives nothing useful)
        # If neither source has a path, no sprite is shown.
        image_hint = entity.image or (npc.image if npc else "")

        # Outer horizontal layout: [sprite] [info column]
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 10, 6)
        outer.setSpacing(8)

        # ── Sprite ──
        icon_path = _resolve_icon(image_hint)
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

        # type / subtype  (e.g. "insect · ant")
        if entity.type_name or entity.subtype:
            parts = [p for p in (entity.type_name, entity.subtype) if p]
            type_lbl = QLabel(" · ".join(parts))
            type_lbl.setStyleSheet(f"color: {OVERLAY}; font-size: 11px; font-style: italic;")
            top.addWidget(type_lbl)

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

        # ── Full field dump tooltip ──
        if entity.all_fields:
            # Organised sections: key identity fields first, then everything else
            priority = ("type", "subtype", "size_category", "unique", "ai",
                        "autolevel", "image", "faction", "rank", "level",
                        "life", "max_life", "combat_armor", "combat_def",
                        "combat_physresist", "combat_spellresist", "combat_mentalresist")
            lines: list[str] = []
            for k in priority:
                if k in entity.all_fields:
                    lines.append(f"{k}: {entity.all_fields[k]!r}")
            for k, v in sorted(entity.all_fields.items()):
                if k not in priority:
                    lines.append(f"{k}: {v!r}")
            self.setToolTip("\n".join(lines))

        # ── Row 4: lore description (truncated) ──
        if npc and npc.desc:
            full_desc = npc.desc
            # Replace newlines with spaces for the inline label
            one_line = " ".join(full_desc.split())
            if len(one_line) > _DESC_MAX_CHARS:
                display_text = one_line[:_DESC_MAX_CHARS].rsplit(" ", 1)[0] + " \u2026"
            else:
                display_text = one_line
            desc_lbl = QLabel(display_text)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                f"color: {SUBTEXT0}; font-size: 11px; font-style: italic;"
            )
            if len(one_line) > _DESC_MAX_CHARS:
                # Full text as tooltip
                self.setToolTip(full_desc)
            info.addWidget(desc_lbl)

        outer.addLayout(info, stretch=1)


class EnemyPanel(QWidget):
    """
    Scrollable enemy list panel.  Call ``update_enemies()`` with a fresh
    entity list whenever the map changes.
    """

    dump_requested = Signal()   # emitted when the user clicks "Dump"

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

        dump_btn = QPushButton("Dump")
        dump_btn.setFixedWidth(48)
        dump_btn.setToolTip("Print all entity fields to the log panel")
        dump_btn.clicked.connect(self.dump_requested)
        hdr_row.addWidget(dump_btn)

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
