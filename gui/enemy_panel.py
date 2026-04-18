"""
enemy_panel.py
--------------
Collapsible enemy list widget for the dashboard.  Shows enemies from
game.level.entities, scanned on map change only.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game_data.npc_db import NpcRecord, get_npc_db, lookup_by_sprite
from gui.memory_reader import (
    DANGER_DANGEROUS,
    DANGER_DEADLY,
    DANGER_EASY,
    DANGER_MODERATE,
    DANGER_TRIVIAL,
    EntityInfo,
)
from gui.sprite_composer import compose_layers
from gui.theme import (
    BLUE,
    BORDER,
    GREEN,
    MAUVE,
    OVERLAY,
    RED,
    SUBTEXT0,
    SURFACE0,
    TEAL,
    TEXT,
    YELLOW,
)

# ── NPC sprite lookup ────────────────────────────────────────────────────────

_ICONS_ROOT = Path(__file__).resolve().parent.parent / "Icons"
_ICON_CACHE: dict[str, Path | None] = {}  # image_hint key → Path or None
_ICON_SIZE = 32
_PREVIEW_SIZE = 160

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
    DANGER_TRIVIAL: OVERLAY,
    DANGER_EASY: GREEN,
    DANGER_MODERATE: YELLOW,
    DANGER_DANGEROUS: "#fab387",  # Catppuccin peach / orange
    DANGER_DEADLY: RED,
}

_RESIST_LABELS: dict[str, str] = {
    "resists.ARCANE": "Arc",
    "resists.BLIGHT": "Blight",
    "resists.COLD": "Cold",
    "resists.DARKNESS": "Dark",
    "resists.FIRE": "Fire",
    "resists.LIGHT": "Light",
    "resists.LIGHTNING": "Lite",
    "resists.NATURE": "Nature",
    "resists.PHYSICAL": "Phys",
    "resists.TEMPORAL": "Temp",
}


def _hp_color(pct: float) -> str:
    if pct > 0.5:
        return GREEN
    if pct > 0.25:
        return YELLOW
    return RED


def _resist_state(value: float) -> tuple[str, str]:
    if value >= 100:
        return "Immune", MAUVE
    if value >= 35:
        return "Heavy", BLUE
    if value <= -1:
        return "Weak", RED
    return "Neutral", SUBTEXT0


def _resist_spans(entity: EntityInfo) -> list[str]:
    spans: list[str] = []
    for key, label in _RESIST_LABELS.items():
        raw = entity.all_fields.get(key)
        if not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        state, color = _resist_state(value)
        text = f"{label} {'Imm' if state == 'Immune' else f'{value:.0f}%'}"
        spans.append(f"<span style='color:{color};'>{text}</span>")
    return spans


def _build_enemy_pixmap(
    entity: EntityInfo,
    image_hint: str,
    size: int,
) -> QPixmap | None:
    """Build a sprite pixmap for an enemy card at the requested size."""
    pix: QPixmap | None = None
    distinct_layers = list(dict.fromkeys(entity.sprite_layers))
    if len(distinct_layers) > 1:
        pix = compose_layers(distinct_layers, size=size)
    if pix is None and image_hint:
        icon_path = _resolve_icon(image_hint)
        if icon_path:
            pix = QPixmap(str(icon_path)).scaled(
                QSize(size, size),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    return pix


class _HoverPreviewLabel(QLabel):
    """Enemy portrait label that shows a larger sprite preview while hovered."""

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preview = QLabel(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self._preview.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            f"background: {SURFACE0};border: 1px solid {BORDER};border-radius: 6px;padding: 8px;"
        )
        self._preview.setPixmap(pixmap)
        self._preview.adjustSize()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._move_preview()
        self._preview.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._preview.hide()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        self._move_preview()
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._preview.hide()
        super().hideEvent(event)

    def _move_preview(self) -> None:
        self._preview.move(QCursor.pos() + QPoint(18, 18))


class _EnemyCard(QFrame):
    """Single enemy row — sprite + name, HP bar, rank badge, key stats, lore."""

    def __init__(self, entity: EntityInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EnemyCard")
        self.setStyleSheet(
            f"#EnemyCard {{  background: {SURFACE0};  border: 1px solid {BORDER};  border-radius: 4px;}}"
        )
        self.setToolTip("")

        # Look up NPC record for lore text, trying three strategies in order:
        # 1. Exact name match (works for generic mobs with their Lua name).
        # 2. Exact image path match (named uniques whose sprite has an explicit
        #    image= field in their Lua block, e.g. dúathedlen → Jaedemas).
        # 3. Shockbolt filename convention: npc/{type}_{subtype}_{name}.png —
        #    catches entities like faerlhing whose Lua has no image= but whose
        #    gfx-pack sprite path encodes the name (→ Neriyamira the Guardian).
        npc_db = get_npc_db()
        entity_name = entity.name.lower()
        npc: NpcRecord | None = npc_db.get(entity_name)
        if npc is None and " (" in entity_name:
            npc = npc_db.get(entity_name.split(" (", 1)[0].strip())
        if npc is None:
            sprite_hints = []
            if entity.image:
                sprite_hints.append(entity.image)
            sprite_hints.extend(entity.sprite_layers)
            for hint in dict.fromkeys(sprite_hints):
                npc = lookup_by_sprite(hint)
                if npc:
                    break

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
        # Composite entities (golem etc.) have ordered layers; single-sprite
        # entities just have image_hint.  compose_layers handles both cases
        # and extracts any missing PNGs from the gfx pack on demand.
        pix = _build_enemy_pixmap(entity, image_hint, _ICON_SIZE)
        preview_pix = _build_enemy_pixmap(entity, image_hint, _PREVIEW_SIZE)
        if pix is not None:
            icon_lbl = _HoverPreviewLabel(preview_pix or pix)
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedSize(_ICON_SIZE, _ICON_SIZE)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_lbl.setMouseTracking(True)
            icon_lbl.setToolTip("")
            outer.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        # ── Info column ──
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(3)

        # Row 1: name + rank badge + danger badge + level + faction
        top = QHBoxLayout()
        top.setSpacing(8)

        rank_color = _RANK_COLORS.get(entity.rank_label, SUBTEXT0)

        danger_color = _DANGER_COLORS.get(entity.danger, OVERLAY)
        danger_badge = QLabel(f" {entity.danger} ")
        danger_badge.setStyleSheet(
            f"background: {danger_color}; color: {SURFACE0}; font-size: 10px;"
            f" font-weight: 700; border-radius: 3px; padding: 1px 6px;"
        )
        top.addWidget(danger_badge)

        rank_badge = QLabel(f" {entity.rank_label} ")
        rank_badge.setStyleSheet(
            f"background: {rank_color}; color: {SURFACE0}; font-size: 10px;"
            f" font-weight: 700; border-radius: 3px; padding: 1px 5px;"
        )
        top.addWidget(rank_badge)

        name_lbl = QLabel(entity.name)
        bold = entity.rank_label in ("Unique", "Boss", "Elite Boss", "Rare")
        weight = "700" if bold else "400"
        name_lbl.setStyleSheet(f"font-weight: {weight}; font-size: 13px; color: {TEXT};")
        top.addWidget(name_lbl)

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

        info.addLayout(top)

        # Row 2: HP text
        pct = entity.life / entity.max_life if entity.max_life > 0 else 0
        hp_color = _hp_color(pct)

        hp_row = QHBoxLayout()
        hp_row.setSpacing(8)

        hp_text = QLabel(f"{entity.life:.0f} / {entity.max_life:.0f}")
        hp_text.setStyleSheet(f"color: {hp_color}; font-size: 11px; font-weight: 600;")
        hp_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hp_row.addWidget(hp_text)
        hp_row.addStretch()

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

        resist_parts = _resist_spans(entity)
        if stats_parts or resist_parts:
            segments: list[str] = []
            if stats_parts:
                segments.append(f"<span style='color:{OVERLAY};'>{'  |  '.join(stats_parts)}</span>")
            if resist_parts:
                segments.append("  |  ".join(resist_parts))
            stats_lbl = QLabel("  |  ".join(segments))
            stats_lbl.setTextFormat(Qt.TextFormat.RichText)
            stats_lbl.setStyleSheet("font-size: 11px;")
            info.addWidget(stats_lbl)

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
            desc_lbl.setStyleSheet(f"color: {SUBTEXT0}; font-size: 11px; font-style: italic;")
            info.addWidget(desc_lbl)

        outer.addLayout(info, stretch=1)


class EnemyPanel(QWidget):
    """
    Scrollable enemy list panel.  Call ``update_enemies()`` with a fresh
    entity list whenever the map changes.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._throbber_frame = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 0, 0)
        outer.setSpacing(6)

        # ── Header ──
        hdr_row = QHBoxLayout()
        title = QLabel("Enemies")
        title.setProperty("heading", True)
        hdr_row.addWidget(title)

        self._map_label = QLabel("")
        self._map_label.setStyleSheet(f"color: {SUBTEXT0}; font-size: 11px;")
        hdr_row.addWidget(self._map_label)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color: {OVERLAY}; font-size: 11px;")
        hdr_row.addWidget(self._count_label)

        hdr_row.addStretch()

        outer.addLayout(hdr_row)

        # ── Scrollable card list ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background: transparent;")

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

        self._loading_label = QLabel("")
        self._loading_label.setStyleSheet(f"color: {YELLOW}; font-size: 13px; font-weight: 700;")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label.setVisible(False)
        outer.addWidget(self._loading_label)

        self._throbber_timer = QTimer(self)
        self._throbber_timer.setInterval(140)
        self._throbber_timer.timeout.connect(self._advance_throbber)

    def set_map_name(self, level_id: str) -> None:
        self._map_label.setText(level_id)

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        if loading:
            self._throbber_frame = 0
            self._empty_label.setVisible(False)
            self._loading_label.setVisible(True)
            self._advance_throbber()
            self._throbber_timer.start()
        else:
            self._throbber_timer.stop()
            self._loading_label.setText("")
            self._loading_label.setVisible(False)

    def _advance_throbber(self) -> None:
        dots = "." * ((self._throbber_frame % 3) + 1)
        self._loading_label.setText(f"Scanning{dots}")
        self._throbber_frame += 1

    def update_enemies(self, enemies: list[EntityInfo]) -> None:
        """Replace the card list with fresh data."""
        self.set_loading(False)
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
