from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from game_data.zone_manifest import ZONES, ZoneEntry
from gui.theme import BG, BORDER, GREEN, SUBTEXT0, SUBTEXT1, SURFACE0, TEXT, YELLOW

_TIER_RANGES = {
    1: "Levels 1–10",
    2: "Levels 10–20",
    3: "Levels 20–30",
    4: "Levels 30–40",
    5: "Levels 40–50",
}

_DOT_UNVISITED = SUBTEXT1
_DOT_VISITED = YELLOW
_DOT_CLEARED = GREEN
_DOT_CURRENT = YELLOW


class _SummaryCard(QFrame):
    def __init__(self, title: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"QFrame {{ background: {SURFACE0}; border: 1px solid {BORDER}; border-radius: 6px; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        title_lbl = QLabel(title.upper())
        title_lbl.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {SUBTEXT1}; letter-spacing: 1px;")
        lay.addWidget(title_lbl)

        self._value_lbl = QLabel("0")
        self._value_lbl.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {accent};")
        lay.addWidget(self._value_lbl)

    def set_value(self, value: str) -> None:
        self._value_lbl.setText(value)


class _ZoneRow(QFrame):
    def __init__(self, zone: ZoneEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._zone = zone
        self.setStyleSheet(f"QFrame {{ background: {SURFACE0}; border: 1px solid {BORDER}; border-radius: 4px; }}")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)

        self._dot = QLabel()
        self._dot.setFixedSize(12, 12)
        self._dot.setStyleSheet(f"background: {_DOT_UNVISITED}; border-radius: 6px;")
        lay.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        name_col = QVBoxLayout()
        name_col.setSpacing(1)
        name_col.setContentsMargins(0, 0, 0, 0)

        self._name_lbl = QLabel(zone.display_name)
        self._name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {SUBTEXT0};")
        name_col.addWidget(self._name_lbl)

        meta_parts: list[str] = []
        if zone.race_req:
            meta_parts.append(f"[{zone.race_req} only]")
        if zone.optional:
            meta_parts.append("[optional]")
        if meta_parts:
            meta_lbl = QLabel("  ".join(meta_parts))
            meta_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT1};")
            name_col.addWidget(meta_lbl)

        lay.addLayout(name_col, 0)

        self._floor_lbl = QLabel("")
        self._floor_lbl.setStyleSheet(f"font-size: 12px; color: {SUBTEXT0};")
        self._floor_lbl.setMinimumWidth(120)
        lay.addWidget(self._floor_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        if zone.notes:
            notes_lbl = QLabel(zone.notes)
            notes_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT1};")
            notes_lbl.setWordWrap(True)
            lay.addWidget(notes_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        else:
            lay.addStretch(1)

    def update_status(
        self,
        visited: bool,
        cleared: bool,
        is_current: bool,
        floor: int,
        max_floor: int,
    ) -> None:
        zone = self._zone

        if cleared:
            dot_color = _DOT_CLEARED
            name_color = GREEN
            floor_text = f"Floor {floor} / {max_floor}" if floor > 0 else f"Floors: {zone.floors}"
        elif is_current:
            dot_color = _DOT_CURRENT
            name_color = YELLOW
            floor_text = f"\u2190 HERE  Floor {floor} / {max_floor}" if floor > 0 else "\u2190 HERE"
        elif visited:
            dot_color = _DOT_VISITED
            name_color = TEXT
            floor_text = f"Floor {floor} / {max_floor}" if floor > 0 else ""
        else:
            dot_color = _DOT_UNVISITED
            name_color = SUBTEXT0
            floor_text = ""

        self._dot.setStyleSheet(f"background: {dot_color}; border-radius: 6px;")
        self._name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {name_color};")
        self._floor_lbl.setText(floor_text)


class ProgressionTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {BG}; }}QWidget {{ background: {BG}; }}")

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(12, 12, 12, 24)
        body_lay.setSpacing(8)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 6)
        summary_row.setSpacing(8)
        self._visited_card = _SummaryCard("Zones Visited", YELLOW)
        self._cleared_card = _SummaryCard("Zones Cleared", GREEN)
        self._uniques_seen_card = _SummaryCard("Uniques Seen", TEXT)
        self._uniques_killed_card = _SummaryCard("Uniques Killed", GREEN)
        for card in (
            self._visited_card,
            self._cleared_card,
            self._uniques_seen_card,
            self._uniques_killed_card,
        ):
            summary_row.addWidget(card, 1)
        body_lay.addLayout(summary_row)

        self._zone_rows: dict[str, _ZoneRow] = {}

        tiers: dict[int, list[ZoneEntry]] = {}
        for zone in ZONES:
            tiers.setdefault(zone.tier, []).append(zone)

        for tier_num in sorted(tiers):
            tier_range = _TIER_RANGES.get(tier_num, "")
            header = QLabel(f"TIER {tier_num}  \u2014  {tier_range}")
            header.setStyleSheet(
                f"font-size: 12px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px; padding: 10px 0 4px 2px;"
            )
            body_lay.addWidget(header)

            for zone in tiers[tier_num]:
                row = _ZoneRow(zone)
                body_lay.addWidget(row)
                if zone.short_name is not None:
                    self._zone_rows[zone.short_name] = row

        body_lay.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def update(
        self,
        visited: set[str],
        deaths: set[str],
        uniques: set[str],
        current_zone: tuple[str, int, int] | None,
    ) -> None:
        current_short = current_zone[0] if current_zone else None
        current_floor = current_zone[1] if current_zone else 0
        current_max = current_zone[2] if current_zone else 0
        cleared_count = 0

        for short_name, row in self._zone_rows.items():
            boss = row._zone.boss
            cleared = boss is not None and boss in deaths
            if cleared:
                cleared_count += 1
            is_current = short_name == current_short
            is_visited = short_name in visited

            if is_current:
                floor = current_floor
                max_floor = current_max
            else:
                floor = 0
                max_floor = row._zone.floors

            row.update_status(
                visited=is_visited,
                cleared=cleared,
                is_current=is_current,
                floor=floor,
                max_floor=max_floor,
            )

        self._visited_card.set_value(str(len(visited)))
        self._cleared_card.set_value(str(cleared_count))
        self._uniques_seen_card.set_value(str(len(uniques)))
        self._uniques_killed_card.set_value(str(len(deaths)))
