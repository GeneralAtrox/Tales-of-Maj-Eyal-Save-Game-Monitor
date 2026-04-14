from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui.theme import (
    BG, BLUE, BORDER, GREEN, MAUVE, OVERLAY,
    SUBTEXT0, SUBTEXT1, SURFACE0, SURFACE1, TEXT, YELLOW,
)

# ── Asset paths ───────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent.parent
TALENT_ICONS = _ROOT / "Icons" / "talents"
STAT_ICONS   = _ROOT / "Icons" / "stats"
CLASS_ICONS  = _ROOT / "Icons" / "class-icons"

# ── Sizes ─────────────────────────────────────────────────────────────────────
_ICON_GRID   = 44   # talent icon in the grid
_ICON_DETAIL = 64   # talent icon in the detail panel
_ICON_STAT   = 28   # stat icon
_ICON_CLASS  = 32   # class icon in header

# ── Stat icon filename mapping ────────────────────────────────────────────────
_STAT_ICONS: dict[str, str] = {
    "Strength":     "strength",
    "Dexterity":    "dexterity",
    "Constitution": "constitution",
    "Magic":        "magic",
    "Willpower":    "willpower",
    "Cunning":      "cunning",
}
_STAT_ORDER = list(_STAT_ICONS)

# ── Icon overrides ────────────────────────────────────────────────────────────
# Talent names whose icon filename doesn't match their snake_case name.
# Value is the stem (no .png) of the file in Icons/talents/.
_ICON_OVERRIDES: dict[str, str] = {
    "Pulverising Auger": "dig",
    "Mirror Image":      "mirror_images",
    "Temporal Shield":   "time_shield",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_snake(name: str) -> str:
    """'Stunning Blow' → 'stunning_blow', "Hunter's Sight" → 'hunters_sight'."""
    s = name.lower().replace("'", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _load_pixmap(path: Path, size: int) -> QPixmap:
    if path.exists():
        px = QPixmap(str(path))
        if not px.isNull():
            return px.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    return _placeholder(size)


def _placeholder(size: int, letter: str = "?") -> QPixmap:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(2, 2, size - 4, size - 4, 6, 6)
    p.fillPath(path, QColor(SURFACE1))
    p.setPen(QColor(OVERLAY))
    f = QFont("Segoe UI", max(size // 4, 7), QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, letter[:1].upper())
    p.end()
    return px


def _is_category_header(value: Any) -> bool:
    """True if the value is a mastery multiplier string like '1.30'."""
    return isinstance(value, str) and bool(re.fullmatch(r"\d+\.\d{2}", value.strip()))


def _level_of(data: Any) -> str:
    """Extract a display-ready level string from talent data."""
    if isinstance(data, dict):
        return str(data.get("Level", "0/5"))
    if isinstance(data, str):
        return data
    return "?"


def _is_zero_level(level_str: str) -> bool:
    return level_str.startswith("0/") or level_str == "0"


# ── Widgets ───────────────────────────────────────────────────────────────────

class _TalentIcon(QWidget):
    """Single clickable talent icon with level badge."""

    clicked = Signal(str, object)   # talent_name, data

    def __init__(self, name: str, data: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._data = data

        level = _level_of(data)
        zero  = _is_zero_level(level)

        self.setFixedSize(_ICON_GRID + 10, _ICON_GRID + 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{name}  [{level}]")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        # Icon
        px = _load_pixmap(TALENT_ICONS / f"{_ICON_OVERRIDES.get(name, _to_snake(name))}.png", _ICON_GRID)
        if zero:
            # Dim the icon for unlearned talents
            dimmed = QPixmap(px.size())
            dimmed.fill(Qt.GlobalColor.transparent)
            p = QPainter(dimmed)
            p.setOpacity(0.35)
            p.drawPixmap(0, 0, px)
            p.end()
            px = dimmed

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(_ICON_GRID, _ICON_GRID)
        icon_lbl.setPixmap(px)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Level text
        level_lbl = QLabel(level)
        level_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        level_color = SUBTEXT0 if zero else GREEN
        level_lbl.setStyleSheet(
            f"font-size: 10px; font-weight: 600; color: {level_color};"
        )

        lay.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(level_lbl)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._name, self._data)
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.setStyleSheet(f"background: {SURFACE1}; border-radius: 6px;")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.setStyleSheet("")
        super().leaveEvent(event)


class _TalentDetailPanel(QWidget):
    """Right-side panel showing the selected talent's details."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(240)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)

        # Icon + name header
        header = QHBoxLayout()
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(_ICON_DETAIL, _ICON_DETAIL)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_col = QVBoxLayout()
        self._name_lbl = QLabel()
        self._name_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {TEXT};"
        )
        self._name_lbl.setWordWrap(True)
        self._category_lbl = QLabel()
        self._category_lbl.setStyleSheet(
            f"font-size: 11px; color: {SUBTEXT0};"
        )
        name_col.addWidget(self._name_lbl)
        name_col.addWidget(self._category_lbl)
        name_col.addStretch()
        header.addWidget(self._icon_lbl)
        header.addSpacing(10)
        header.addLayout(name_col, 1)
        root.addLayout(header)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {BORDER}; margin-top: 10px; margin-bottom: 10px;")
        root.addWidget(div)

        # Fields area (rebuilt on each talent click)
        self._fields_w = QWidget()
        self._fields_lay = QVBoxLayout(self._fields_w)
        self._fields_lay.setContentsMargins(0, 4, 0, 0)
        self._fields_lay.setSpacing(5)
        root.addWidget(self._fields_w)
        root.addStretch()

        self._show_placeholder()

    # ── Field color map ───────────────────────────────────────────────────
    _COLORS: dict[str, str] = {
        "Level":        GREEN,
        "Range":        BLUE,
        "Cooldown":     YELLOW,
        "Travel Speed": SUBTEXT1,
        "Usage Speed":  SUBTEXT1,
        "Scales With":  MAUVE,
        "Turn Duration":GREEN,
        "Stats":        TEXT,
        "Stats per turn": TEXT,
    }
    _ORDER = [
        "Level", "Range", "Cooldown", "Travel Speed",
        "Usage Speed", "Scales With", "Turn Duration",
        "Stats", "Stats per turn",
    ]

    def show_talent(self, name: str, data: Any, category: str = "") -> None:
        self._clear_fields()

        px = _load_pixmap(TALENT_ICONS / f"{_ICON_OVERRIDES.get(name, _to_snake(name))}.png", _ICON_DETAIL)
        self._icon_lbl.setPixmap(px)
        self._name_lbl.setText(name)
        self._category_lbl.setText(category)

        if isinstance(data, str):
            self._add_field("Level", data, GREEN)
            return

        for key in self._ORDER:
            if key not in data:
                continue
            val = data[key]
            if isinstance(val, list):
                val = "\n".join(f"• {v}" for v in val)
            self._add_field(key, str(val), self._COLORS.get(key, TEXT))

    def _add_field(self, label: str, value: str, color: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(f"{label}")
        lbl.setFixedWidth(95)
        lbl.setStyleSheet(f"color: {SUBTEXT0}; font-size: 12px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        val = QLabel(value)
        val.setStyleSheet(f"color: {color}; font-size: 12px;")
        val.setWordWrap(True)
        val.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(lbl)
        row.addWidget(val, 1)
        container = QWidget()
        container.setLayout(row)
        self._fields_lay.addWidget(container)

    def _clear_fields(self) -> None:
        while self._fields_lay.count():
            item = self._fields_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_placeholder(self) -> None:
        self._icon_lbl.setPixmap(_placeholder(_ICON_DETAIL, "?"))
        self._name_lbl.setText("Select a talent")
        self._category_lbl.setText("")


class _CategoryHeader(QLabel):
    """Inline category separator inside a talent section."""

    def __init__(self, name: str, mastery: str, parent: QWidget | None = None) -> None:
        display = f"  {name}  (×{mastery})"
        super().__init__(display, parent)
        self.setStyleSheet(
            f"color: {YELLOW};"
            f" font-size: 11px;"
            f" font-weight: 600;"
            f" padding: 4px 0 2px 0;"
            f" letter-spacing: 0.3px;"
        )


class _StatsRow(QWidget):
    """Row of six stat icons with current values."""

    def __init__(self, stats: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(16)

        for stat_name in _STAT_ORDER:
            raw = stats.get(stat_name, "")
            if not raw:
                continue
            current = raw.split(" ")[0]

            col = QVBoxLayout()
            col.setSpacing(2)
            col.setContentsMargins(0, 0, 0, 0)

            px = _load_pixmap(STAT_ICONS / f"{_STAT_ICONS[stat_name]}.png", _ICON_STAT)
            ico = QLabel()
            ico.setPixmap(px)
            ico.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_lbl = QLabel(current)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_lbl.setStyleSheet(
                f"font-size: 13px; font-weight: 700; color: {TEXT};"
            )

            abbr = QLabel(stat_name[:3].upper())
            abbr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            abbr.setStyleSheet(f"font-size: 10px; color: {SUBTEXT0};")

            col.addWidget(ico, alignment=Qt.AlignmentFlag.AlignCenter)
            col.addWidget(val_lbl)
            col.addWidget(abbr)

            w = QWidget()
            w.setLayout(col)
            lay.addWidget(w)

        lay.addStretch()


# ── Main widget ───────────────────────────────────────────────────────────────

class CharacterSheetView(QWidget):
    """Visual character sheet: stats row + icon talent grid + detail panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detail_panel = _TalentDetailPanel()
        self._current_category: str = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────
        self._header = _HeaderBar()
        root.addWidget(self._header)

        # ── Splitter: left scroll | right detail ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter)

        # Left: scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: {BG}; }}"
            f"QWidget {{ background: {BG}; }}"
        )
        self._left = QWidget()
        self._left_lay = QVBoxLayout(self._left)
        self._left_lay.setContentsMargins(12, 12, 12, 24)
        self._left_lay.setSpacing(2)
        self._left_lay.addStretch()
        scroll.setWidget(self._left)
        splitter.addWidget(scroll)

        # Right: detail panel
        splitter.addWidget(self._detail_panel)
        splitter.setSizes([700, 300])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

    # ── Public API ────────────────────────────────────────────────────────

    def load(self, data: dict, char_name: str = "") -> None:
        self._clear_left()
        char = data.get("Character", {})
        self._header.update_from(char, char_name)

        # Stats row
        stats = data.get("Primary Stats", {})
        if stats:
            self._insert(self._left_lay, _StatsRow(stats))
            self._insert(self._left_lay, _divider())

        # Talent sections
        for key, value in data.items():
            if "Talents" not in key or not isinstance(value, dict) or not value:
                continue
            self._insert(self._left_lay, self._build_talent_section(key, value))

    # ── Builders ─────────────────────────────────────────────────────────

    def build_talent_section(self, title: str, talents: dict) -> QWidget:
        return self._build_talent_section(title, talents)

    def _build_talent_section(self, title: str, talents: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 4)
        lay.setSpacing(4)

        # Section title (e.g. "CLASS TALENTS")
        sec_lbl = QLabel(title.upper())
        sec_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0};"
            f" letter-spacing: 1px; padding-bottom: 2px;"
        )
        lay.addWidget(sec_lbl)

        # Walk talents — category headers interspersed with talent entries
        current_category = ""
        current_grid: list[tuple[str, Any]] = []

        def flush_grid(grid_items: list[tuple[str, Any]]) -> None:
            if not grid_items:
                return
            gw = QWidget()
            gl = QGridLayout(gw)
            gl.setContentsMargins(0, 2, 0, 6)
            gl.setSpacing(4)
            for idx, (t_name, t_data) in enumerate(grid_items):
                icon_w = _TalentIcon(t_name, t_data)
                icon_w.clicked.connect(
                    lambda n, d, cat=current_category: self._on_talent_clicked(n, d, cat)
                )
                gl.addWidget(icon_w, idx // 4, idx % 4)
            lay.addWidget(gw)

        for name, data in talents.items():
            if _is_category_header(data):
                flush_grid(current_grid)
                current_grid = []
                current_category = name
                lay.addWidget(_CategoryHeader(name, data))
            else:
                current_grid.append((name, data))

        flush_grid(current_grid)
        return w

    def _on_talent_clicked(self, name: str, data: Any, category: str) -> None:
        self._detail_panel.show_talent(name, data, category)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _clear_left(self) -> None:
        while self._left_lay.count() > 1:  # keep trailing stretch
            item = self._left_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _insert(layout: QVBoxLayout, widget: QWidget) -> None:
        layout.insertWidget(layout.count() - 1, widget)


class _HeaderBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setStyleSheet(f"background: {SURFACE1}; border-bottom: 1px solid {BORDER};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

        self._class_icon = QLabel()
        self._class_icon.setFixedSize(_ICON_CLASS, _ICON_CLASS)
        self._class_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._info_lbl = QLabel("No character loaded")
        self._info_lbl.setStyleSheet(f"font-size: 13px; color: {SUBTEXT0};")

        lay.addWidget(self._class_icon)
        lay.addWidget(self._info_lbl, 1)

    def update_from(self, char: dict[str, str], char_name: str = "") -> None:
        cls   = char.get("Class", "")
        race  = char.get("Race", "")
        level = char.get("Level / Exp", "").split(" ")[0]
        mode  = char.get("Mode", "")

        # Class icon
        if cls:
            icon_file = CLASS_ICONS / f"{_to_snake(cls)}_32_bg.png"
            px = _load_pixmap(icon_file, _ICON_CLASS)
            self._class_icon.setPixmap(px)

        parts = [p for p in [char_name or cls, race, f"Level {level}" if level else "", mode] if p]
        self._info_lbl.setText("   ·   ".join(parts))


def _divider() -> QFrame:
    div = QFrame()
    div.setFrameShape(QFrame.Shape.HLine)
    div.setStyleSheet(f"color: {BORDER}; margin: 4px 0;")
    return div
