"""
enemy_panel.py
--------------
Collapsible enemy list widget for the dashboard.  Shows enemies from
game.level.entities, scanned on map change only.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
    PlayerStats,
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
from scoring.battle_simulator import combined_threat_pct, threat_tier_label
from scoring.combat_advice import survive_one_hit_advice
from scoring.enemy_threat import EnemyOffense, PlayerDefenses, ThreatReport, weapon_threat
from scoring.talent_threat import (
    TalentThreatReport,
    compute_talent_threat,
    enemy_powers_from_fields,
    talent_timing_label,
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

# New threat-tier colours (damage-based, % of effective HP)
_THREAT_TIER_COLORS: dict[str, str] = {
    "Low": OVERLAY,
    "Mediocre": SUBTEXT0,
    "High": "#fab387",
    "Deadly": RED,
}


def player_stats_to_defenses(stats: PlayerStats | None) -> PlayerDefenses | None:
    """Adapter — `PlayerStats` (memory_reader) → `PlayerDefenses` (scoring).

    Returns None when we have no usable data, so the panel can fall
    back to rank-based rating.
    """
    if stats is None or stats.max_life <= 0:
        return None
    return PlayerDefenses(
        max_life=stats.max_life,
        die_at=stats.die_at,
        armor=stats.armor,
        armor_hardiness_pct=stats.armor_hardiness,
        defense=stats.defense,
        evasion_pct=stats.evasion,
        resists=dict(stats.resists),
        resists_pen=dict(stats.resists_pen),
        resists_cap=dict(stats.resists_cap),
        ignore_direct_crits_pct=stats.ignore_direct_crits,
    )

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

    def __init__(
        self,
        entity: EntityInfo,
        parent: QWidget | None = None,
        *,
        player: PlayerDefenses | None = None,
        on_simulate: Callable[[], None] | None = None,
    ) -> None:
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

        # Compute threat report if we have player defenses; else fall back to
        # the rank-based danger label.
        report: ThreatReport | None = None
        talent_report: TalentThreatReport | None = None
        talent_can_kill = False
        if player is not None:
            enemy_offense = EnemyOffense.from_all_fields(entity.all_fields, entity.name)
            report = weapon_threat(enemy_offense, player)
            talent_report = compute_talent_threat(enemy_powers_from_fields(entity.all_fields), player)

        if report is not None:
            threat_pct = combined_threat_pct(report, talent_report)
            tier = threat_tier_label(threat_pct)
            tier_color = _THREAT_TIER_COLORS.get(tier, OVERLAY)
            badge_text = f" {tier} {threat_pct:.0f}% "
            talent_line = ""
            if talent_report is not None and talent_report.max_expected_damage > 0.0:
                talent_name = talent_report.worst_talent_name or talent_report.worst_talent_id
                timing = talent_timing_label(
                    talent_report.worst_mode,
                    talent_report.worst_cooldown,
                    talent_report.worst_current_cooldown,
                )
                timing_text = f", {timing}" if timing else ""
                talent_line = (
                    f"\nTalent threat: {talent_name} {talent_report.max_expected_damage:.0f} "
                    f"{talent_report.worst_damage_type or 'all'} "
                    f"({talent_report.max_threat_pct:.0f}% HP{timing_text})"
                )
            tooltip = (
                f"Threat score: {threat_pct:.0f}% of effective HP\n"
                f"Weapon threat: {report.weapon_threat_pct:.0f}% of effective HP\n"
                f"Expected single-hit damage: {report.expected_damage:.0f}\n"
                f"Peak single-hit damage: {report.peak_damage:.0f}\n"
                f"Peak weapon burst: {report.burst_peak_damage:.0f}"
                f"{f' across {report.burst_hits} hits' if report.burst_hits > 1 else ''}\n"
                f"Hit rate: {report.hit_rate_pct:.0f}%\n"
                f"Damage type: {report.damage_type} "
                f"(x{report.worst_resist_multiplier:.2f})"
                f"{talent_line}"
            )
            danger_badge = QLabel(badge_text)
            danger_badge.setToolTip(tooltip)
            danger_badge.setStyleSheet(
                f"background: {tier_color}; color: {SURFACE0}; font-size: 10px;"
                f" font-weight: 700; border-radius: 3px; padding: 1px 6px;"
            )
            top.addWidget(danger_badge)

            available_talent = talent_report.strongest_available_entry() if talent_report is not None else None
            talent_can_kill = available_talent is not None and available_talent.expected_damage >= player.effective_hp
            if report.can_one_shot or report.can_burst_kill or talent_can_kill:
                if report.can_one_shot:
                    badge_label = " 1-SHOT "
                    badge_tooltip = "This enemy can remove all your HP in a single hit."
                elif report.can_burst_kill:
                    badge_label = " BURST "
                    badge_tooltip = "This enemy can remove all your HP with a multi-hit weapon talent."
                else:
                    badge_label = " TALENT "
                    badge_tooltip = "This enemy can remove all your HP with an available non-weapon talent."
                oneshot = QLabel(badge_label)
                oneshot.setToolTip(badge_tooltip)
                oneshot.setStyleSheet(
                    f"background: {RED}; color: {SURFACE0}; font-size: 10px;"
                    f" font-weight: 800; border-radius: 3px; padding: 1px 6px;"
                )
                top.addWidget(oneshot)
        else:
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

        # ── Row 3b: combat advice (only when threat is high enough) ──
        talent_pressure_high = talent_report is not None and talent_report.max_threat_pct >= 70
        if report is not None and player is not None:
            if report.can_one_shot or report.weapon_threat_pct >= 70:
                advice_items = survive_one_hit_advice(enemy_offense, player)
                if advice_items:
                    feasible = [a for a in advice_items if a.feasible]
                    chosen = feasible[0] if feasible else advice_items[0]
                    prefix = "⚠ " if report.can_one_shot else "● "
                    text = f"{prefix}{chosen.description}"
                    if not chosen.feasible and not feasible:
                        text += "  (no single lever saves you — stack defenses)"
                    advice_lbl = QLabel(text)
                    advice_lbl.setWordWrap(True)
                    color = RED if report.can_one_shot else YELLOW
                    advice_lbl.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
                    info.addWidget(advice_lbl)
            if talent_report is not None and (talent_can_kill or talent_pressure_high):
                talent_name = talent_report.worst_talent_name or talent_report.worst_talent_id
                timing = talent_timing_label(
                    talent_report.worst_mode,
                    talent_report.worst_cooldown,
                    talent_report.worst_current_cooldown,
                )
                timing_text = f", {timing}" if timing else ""
                prefix = "⚠ " if talent_can_kill else "● "
                text = (
                    f"{prefix}Strongest talent: {talent_name} "
                    f"({talent_report.max_expected_damage:.0f} "
                    f"{talent_report.worst_damage_type or 'all'}{timing_text})"
                )
                talent_lbl = QLabel(text)
                talent_lbl.setWordWrap(True)
                color = RED if talent_can_kill else YELLOW
                talent_lbl.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
                info.addWidget(talent_lbl)

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

        if on_simulate is not None:
            action_row = QHBoxLayout()
            action_row.setContentsMargins(0, 2, 0, 0)
            action_row.addStretch()
            simulate_btn = QPushButton("Add To Battle Simulator")
            simulate_btn.clicked.connect(on_simulate)
            action_row.addWidget(simulate_btn, 0, Qt.AlignmentFlag.AlignRight)
            info.addLayout(action_row)

        outer.addLayout(info, stretch=1)


class EnemyPanel(QWidget):
    """
    Scrollable enemy list panel.  Call ``update_enemies()`` with a fresh
    entity list whenever the map changes.
    """

    simulate_requested = Signal(object)

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

    def update_enemies(
        self,
        enemies: list[EntityInfo],
        player: PlayerDefenses | None = None,
    ) -> None:
        """Replace the card list with fresh data.

        `player` enables per-enemy damage threat math. When omitted, the
        panel falls back to the rank-based danger label.
        """
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
            card = _EnemyCard(
                ent,
                player=player,
                on_simulate=lambda checked=False, enemy=ent: self.simulate_requested.emit(enemy),
            )
            # Insert before the trailing stretch
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)
