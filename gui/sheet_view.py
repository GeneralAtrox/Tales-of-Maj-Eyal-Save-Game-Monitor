from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.theme import (
    BG, BLUE, BORDER, GREEN, MAUVE, OVERLAY, RED, TEAL,
    SUBTEXT0, SUBTEXT1, SURFACE0, SURFACE1, SURFACE2, TEXT, YELLOW,
)
from gui.sprite_composer import compose_layers, get_sprite, normalize_sprite_layers

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
_ICON_SPRITE = 192  # live player sprite beside primary stats

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
    "Pulverising Auger":     "dig",
    "Pulverizing Auger":     "dig",
    "Mirror Image":          "mirror_images",
    "Temporal Shield":       "time_shield",
    "Arcane Reconstruction": "heal",
    "Ogric Wrath":           "ogre_wrath",
    "Heavy Armour Training": "armour_training",
    "Combat Accuracy":       "weapon_combat",
    "Dagger Mastery":        "knife_mastery",
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


def _load_stat_pixmap(path: Path, size: int) -> QPixmap:
    """Load stat icons with black matte pixels converted to transparency."""
    if not path.exists():
        return _placeholder(size)

    image = QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32)
    if image.isNull():
        return _placeholder(size)

    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() == 0:
                continue
            if color.red() <= 10 and color.green() <= 10 and color.blue() <= 10:
                color.setAlpha(0)
                image.setPixelColor(x, y, color)

    # Some legacy stat icons include a bottom underline/shadow band. First
    # strip dark pixels connected to the bottom edge, then detect and remove a
    # residual thin bottom band regardless of colour.
    width = image.width()
    height = image.height()
    queue: deque[tuple[int, int]] = deque()
    seen: set[tuple[int, int]] = set()

    def is_dark(x: int, y: int) -> bool:
        color = image.pixelColor(x, y)
        return (
            color.alpha() > 0
            and color.red() <= 40
            and color.green() <= 40
            and color.blue() <= 40
        )

    for x in range(width):
        if is_dark(x, height - 1):
            queue.append((x, height - 1))
            seen.add((x, height - 1))

    while queue:
        x, y = queue.popleft()
        color = image.pixelColor(x, y)
        color.setAlpha(0)
        image.setPixelColor(x, y, color)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx = x + dx
            ny = y + dy
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            if (nx, ny) in seen or not is_dark(nx, ny):
                continue
            seen.add((nx, ny))
            queue.append((nx, ny))

    top = height
    bottom = -1
    left = width
    right = -1
    for y in range(height):
        for x in range(width):
            if image.pixelColor(x, y).alpha() == 0:
                continue
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)

    if bottom >= top:
        row_counts: list[int] = []
        for y in range(top, bottom + 1):
            count = 0
            for x in range(left, right + 1):
                if image.pixelColor(x, y).alpha() > 0:
                    count += 1
            row_counts.append(count)

        bottom_band = row_counts[-1]
        removable_rows = 0
        if bottom_band >= max(8, int((right - left + 1) * 0.45)):
            removable_rows = 1
            if len(row_counts) >= 2 and row_counts[-2] >= max(8, int((right - left + 1) * 0.35)):
                removable_rows = 2

        if removable_rows:
            above_index = len(row_counts) - removable_rows - 1
            above_count = row_counts[above_index] if above_index >= 0 else 0
            if above_count <= max(3, int(bottom_band * 0.45)):
                for y in range(bottom - removable_rows + 1, bottom + 1):
                    for x in range(left, right + 1):
                        color = image.pixelColor(x, y)
                        color.setAlpha(0)
                        image.setPixelColor(x, y, color)

    pixmap = _trim_transparent_bounds(QPixmap.fromImage(image))
    return pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


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


def _trim_transparent_bounds(pixmap: QPixmap) -> QPixmap:
    """Crop transparent padding around a pixmap so sprites sit tighter in layout."""
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    left = image.width()
    top = image.height()
    right = -1
    bottom = -1

    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() == 0:
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)

    if right < left or bottom < top:
        return pixmap
    return pixmap.copy(left, top, right - left + 1, bottom - top + 1)


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
        self._name_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._category_lbl = QLabel()
        self._category_lbl.setStyleSheet(
            f"font-size: 11px; color: {SUBTEXT0};"
        )
        self._category_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
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
        "Description":  SUBTEXT1,
    }
    _ORDER = [
        "Level", "Range", "Cooldown", "Travel Speed",
        "Usage Speed", "Scales With", "Turn Duration",
        "Stats", "Stats per turn", "Description",
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
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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

    def __init__(
        self,
        stats: dict[str, str],
        sprite: QPixmap | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 4)
        lay.setSpacing(12)

        if sprite is not None:
            sprite_lbl = QLabel()
            sprite_lbl.setPixmap(sprite)
            sprite_lbl.setFixedSize(sprite.size())
            sprite_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            lay.addWidget(sprite_lbl, 0, Qt.AlignmentFlag.AlignTop)

        for stat_name in _STAT_ORDER:
            raw = stats.get(stat_name, "")
            if not raw:
                continue
            current = raw.split(" ")[0]

            col = QVBoxLayout()
            col.setSpacing(2)
            col.setContentsMargins(0, 0, 0, 0)

            px = _load_stat_pixmap(STAT_ICONS / f"{_STAT_ICONS[stat_name]}.png", _ICON_STAT)
            ico = QLabel()
            ico.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            ico.setStyleSheet("background: transparent; border: none;")
            ico.setFixedSize(_ICON_STAT, _ICON_STAT)
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
            w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            w.setStyleSheet("background: transparent; border: none;")
            w.setLayout(col)
            lay.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)

        lay.addStretch()


def _item_rank_color(item: dict[str, Any]) -> str:
    tags = {str(tag).lower() for tag in item.get("Tags", [])}
    if "plot item" in tags:
        return RED
    if "unique" in tags:
        return YELLOW
    if "rare" in tags:
        return MAUVE
    if "ego" in tags:
        return BLUE
    tier = item.get("Tier")
    if isinstance(tier, int) and tier >= 5:
        return YELLOW
    if isinstance(tier, int) and tier >= 3:
        return TEAL
    return SUBTEXT1


def _item_rank_score(item: dict[str, Any]) -> int:
    tags = {str(tag).lower() for tag in item.get("Tags", [])}
    if "plot item" in tags:
        return 0
    if "unique" in tags:
        return 1
    if "rare" in tags:
        return 2
    if "ego" in tags:
        return 3
    tier = item.get("Tier")
    if isinstance(tier, int):
        return max(4, 10 - min(tier, 6))
    return 10


_SLOT_ORDER = {
    "Mainhand": 0,
    "Offhand": 1,
    "Quiver": 2,
    "Psiblades": 3,
    "Body": 4,
    "Head": 5,
    "Hands": 6,
    "Feet": 7,
    "Belt": 8,
    "Neck": 9,
    "Left ring": 10,
    "Right ring": 11,
    "Lite": 12,
    "Tool": 13,
}


def _slot_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    slot = str(item.get("Slot") or "")
    return _SLOT_ORDER.get(slot, 99), slot.lower()


def _inventory_sort_key(item: dict[str, Any]) -> tuple[int, str, str, str]:
    return (
        _item_rank_score(item),
        str(item.get("Type") or "").lower(),
        _display_item_name(item).lower(),
        str(item.get("Name") or "").lower(),
    )


def _item_property(item: dict[str, Any], *keys: str) -> str | None:
    props = item.get("Properties")
    if not isinstance(props, dict):
        return None
    for key in keys:
        value = props.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)
    return None


def _display_item_name(item: dict[str, Any]) -> str:
    name = str(item.get("Name") or "Unknown Item")
    return re.sub(r"^\d+\s+", "", name).strip()


def _item_summary_fields(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    count = item.get("Count")
    if isinstance(count, int) and count > 1:
        parts.append(f"Ammo x{count}")

    if power := _item_property(item, "Base power", "Physical power", "Spellpower", "Mindpower"):
        parts.append(f"Power {power}")
    if apr := _item_property(item, "Armour penetration", "Armour Penetration"):
        parts.append(f"APR {apr}")
    if defense := _item_property(item, "Defense", "Ranged Defense"):
        parts.append(f"Def {defense}")
    if armour := _item_property(item, "Armour"):
        parts.append(f"Armour {armour}")
    if block := _item_property(item, "Block", "On shield block", "Capacity"):
        parts.append(f"Block {block}")
    encumbrance = item.get("Encumbrance")
    if isinstance(encumbrance, (int, float)):
        parts.append(f"Enc {encumbrance:g}")
    return parts


class _ItemCard(QFrame):
    def __init__(self, item: dict[str, Any], *, show_slot: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        accent = _item_rank_color(item)
        self.setStyleSheet(
            f"background: {SURFACE0}; border: 1px solid {BORDER}; border-left: 3px solid {accent}; border-radius: 4px;"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        if show_slot and item.get("Slot"):
            slot_lbl = QLabel(str(item["Slot"]))
            slot_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            title_row.addWidget(slot_lbl)

        name_lbl = QLabel(_display_item_name(item))
        name_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {_item_rank_color(item)};"
        )
        title_row.addWidget(name_lbl, 1)

        meta_parts: list[str] = []
        if isinstance(item.get("Tier"), int):
            meta_parts.append(f"Tier {item['Tier']}")
        tags = item.get("Tags")
        if isinstance(tags, list) and tags:
            meta_parts.append(" ".join(f"[{tag}]" for tag in tags))
        if meta_parts:
            meta_lbl = QLabel("  ".join(meta_parts))
            meta_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            title_row.addWidget(meta_lbl)
        lay.addLayout(title_row)

        summary = _item_summary_fields(item)
        if summary:
            summary_lbl = QLabel("  |  ".join(summary))
            summary_lbl.setWordWrap(True)
            summary_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT};")
            lay.addWidget(summary_lbl)


class _ItemListPanel(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 0, 0)
        root.setSpacing(6)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;"
        )
        root.addWidget(title_lbl)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: {BG}; }}"
            f"QWidget {{ background: {BG}; }}"
        )
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 8)
        self._body_lay.setSpacing(6)
        self._body_lay.addStretch()
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

    def set_items(self, items: list[Any], *, show_slot: bool) -> None:
        while self._body_lay.count() > 1:
            item = self._body_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not items:
            empty = QLabel("No items")
            empty.setStyleSheet(f"font-size: 12px; color: {SUBTEXT0}; padding: 8px 4px;")
            self._body_lay.insertWidget(0, empty)
            return
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                item = {"Name": item}
            if not isinstance(item, dict):
                continue
            normalized_items.append(item)

        sort_key = _slot_sort_key if show_slot else _inventory_sort_key
        for item in sorted(normalized_items, key=sort_key):
            self._body_lay.insertWidget(self._body_lay.count() - 1, _ItemCard(item, show_slot=show_slot))


# ── Main widget ───────────────────────────────────────────────────────────────

class CharacterSheetView(QWidget):
    """Visual character sheet: stats row + icon talent grid + detail panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detail_panel = _TalentDetailPanel()
        self._current_category: str = ""
        self._current_sprite: QPixmap | None = None
        self._current_sprite_key: tuple[str, tuple[str, ...]] | None = None
        self._current_data: dict[str, Any] = {}
        self._current_char_name = ""
        self._live_equipment: list[dict[str, Any]] | None = None
        self._live_inventory: list[dict[str, Any]] | None = None
        self._live_transmog: list[dict[str, Any]] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────
        self._header = _HeaderBar()
        root.addWidget(self._header)

        # ── Fixed player overview ────────────────────────────────────────
        self._player_box = QFrame()
        self._player_box.setStyleSheet(
            f"background: {BG}; border-bottom: 1px solid {BORDER};"
        )
        self._player_box_lay = QVBoxLayout(self._player_box)
        self._player_box_lay.setContentsMargins(12, 0, 12, 8)
        self._player_box_lay.setSpacing(0)
        root.addWidget(self._player_box)

        # ── Content tabs ─────────────────────────────────────────────────
        self._content_tabs = QTabWidget()
        root.addWidget(self._content_tabs, 1)

        # Talents tab: left scroll | right detail
        talents_tab = QWidget()
        talents_root = QVBoxLayout(talents_tab)
        talents_root.setContentsMargins(0, 0, 0, 0)
        talents_root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
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
        splitter.addWidget(self._detail_panel)
        splitter.setSizes([700, 300])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        talents_root.addWidget(splitter)
        self._content_tabs.addTab(talents_tab, "Talents")

        # Inventory tab: equipped | current+transmog
        inventory_tab = QWidget()
        inventory_root = QVBoxLayout(inventory_tab)
        inventory_root.setContentsMargins(0, 0, 0, 0)
        inventory_root.setSpacing(0)

        inventory_splitter = QSplitter(Qt.Orientation.Horizontal)
        inventory_splitter.setHandleWidth(1)
        inventory_splitter.setChildrenCollapsible(False)
        self._equipped_panel = _ItemListPanel("EQUIPPED")
        right_inventory = QSplitter(Qt.Orientation.Vertical)
        right_inventory.setHandleWidth(1)
        right_inventory.setChildrenCollapsible(False)
        self._inventory_panel = _ItemListPanel("CURRENT")
        self._transmog_panel = _ItemListPanel("TRANSMOG CHEST")
        right_inventory.addWidget(self._inventory_panel)
        right_inventory.addWidget(self._transmog_panel)
        right_inventory.setStretchFactor(0, 1)
        right_inventory.setStretchFactor(1, 1)
        right_inventory.setSizes([300, 220])
        inventory_splitter.addWidget(self._equipped_panel)
        inventory_splitter.addWidget(right_inventory)
        inventory_splitter.setSizes([420, 520])
        inventory_splitter.setStretchFactor(0, 1)
        inventory_splitter.setStretchFactor(1, 1)
        inventory_root.addWidget(inventory_splitter)
        self._content_tabs.addTab(inventory_tab, "Inventory")

        from gui.progression_tab import ProgressionTab
        self._progression_tab = ProgressionTab()
        self._content_tabs.addTab(self._progression_tab, "Progression")

        self._enemy_host = QWidget()
        self._enemy_host_lay = QVBoxLayout(self._enemy_host)
        self._enemy_host_lay.setContentsMargins(0, 0, 0, 0)
        self._enemy_host_lay.setSpacing(0)
        self._content_tabs.addTab(self._enemy_host, "Enemies")

    # ── Public API ────────────────────────────────────────────────────────

    def set_hp(self, life: float, max_life: float) -> None:
        self._header.set_hp(life, max_life)

    def clear_hp(self) -> None:
        self._header.clear_hp()

    def set_mana(self, mana: float, max_mana: float) -> None:
        self._header.set_mana(mana, max_mana)

    def clear_mana(self) -> None:
        self._header.clear_mana()

    def set_exp(self, exp: float, needed: float) -> None:
        self._header.set_exp(exp, needed)

    def clear_exp(self) -> None:
        self._header.clear_exp()

    def set_character_menu(self, menu) -> None:
        self._header.set_character_menu(menu)

    def set_actions_menu(self, menu) -> None:
        self._header.set_actions_menu(menu)

    def set_sprite(self, image_hint: str, sprite_layers: list[str] | None = None) -> None:
        sprite_key = (image_hint, tuple(sprite_layers or ()))
        if sprite_key == self._current_sprite_key:
            return
        self._current_sprite_key = sprite_key
        self._current_sprite = self._render_sprite(image_hint, sprite_layers or [])
        self._reload_current()

    def clear_sprite(self) -> None:
        if self._current_sprite is None and self._current_sprite_key is None:
            return
        self._current_sprite = None
        self._current_sprite_key = None
        self._reload_current()

    def load(self, data: dict, char_name: str = "") -> None:
        self._current_data = data
        self._current_char_name = char_name
        self._reload_current()

    def set_live_inventory(
        self,
        equipment: list[dict[str, Any]] | None,
        current: list[dict[str, Any]] | None,
        transmog: list[dict[str, Any]] | None,
    ) -> None:
        self._live_equipment = equipment
        self._live_inventory = current
        self._live_transmog = transmog
        self._reload_current()

    def clear_live_inventory(self) -> None:
        if (
            self._live_equipment is None
            and self._live_inventory is None
            and self._live_transmog is None
        ):
            return
        self._live_equipment = None
        self._live_inventory = None
        self._live_transmog = None
        self._reload_current()

    def _reload_current(self) -> None:
        self._clear_left()
        self._clear_player_box()
        char = self._current_data.get("Character", {})
        self._header.update_from(char, self._current_char_name)

        # Stats row
        stats = self._current_data.get("Primary Stats", {})
        if stats:
            self._player_box_lay.addWidget(_StatsRow(stats, sprite=self._current_sprite))

        # Talent sections
        for key, value in self._current_data.items():
            if "Talents" not in key or not isinstance(value, dict) or not value:
                continue
            self._insert(self._left_lay, self._build_talent_section(key, value))

        file_equipment = self._current_data.get("Equipment", [])
        inventory = self._current_data.get("Inventory", [])
        equipment_items = self._live_equipment if self._live_equipment is not None else (
            file_equipment if isinstance(file_equipment, list) else []
        )
        self._equipped_panel.set_items(equipment_items, show_slot=True)
        if isinstance(inventory, dict):
            file_current = inventory.get("Current", [])
            transmog_items = inventory.get("Transmog Chest", [])
        elif isinstance(inventory, list):
            file_current = inventory
            transmog_items = []
        else:
            file_current = []
            transmog_items = []
        current_items = self._live_inventory if self._live_inventory is not None else file_current
        transmog_live = self._live_transmog if self._live_transmog is not None else transmog_items
        self._inventory_panel.set_items(current_items if isinstance(current_items, list) else [], show_slot=False)
        self._transmog_panel.set_items(transmog_live if isinstance(transmog_live, list) else [], show_slot=False)

    def set_enemy_panel(self, panel: QWidget) -> None:
        while self._enemy_host_lay.count():
            item = self._enemy_host_lay.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._enemy_host_lay.addWidget(panel)

    def update_progression(
        self,
        visited: set[str],
        deaths: set[str],
        current_zone: tuple[str, int, int] | None,
    ) -> None:
        self._progression_tab.update(visited, deaths, current_zone)

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

    def _clear_player_box(self) -> None:
        while self._player_box_lay.count():
            item = self._player_box_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _insert(layout: QVBoxLayout, widget: QWidget) -> None:
        layout.insertWidget(layout.count() - 1, widget)

    def _render_sprite(self, image_hint: str, sprite_layers: list[str]) -> QPixmap | None:
        distinct_layers, align_bottom = normalize_sprite_layers(image_hint, sprite_layers)

        if distinct_layers:
            pix = compose_layers(distinct_layers, size=_ICON_SPRITE, align_bottom=align_bottom)
            if pix is not None:
                return _trim_transparent_bounds(pix)

        if image_hint:
            sprite_path = get_sprite(image_hint)
            if sprite_path:
                raw = QPixmap(str(sprite_path))
                if not raw.isNull():
                    return _trim_transparent_bounds(raw.scaled(
                        _ICON_SPRITE,
                        _ICON_SPRITE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation,
                    ))
        return None


class _HeaderBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setStyleSheet(f"background: {SURFACE1}; border-bottom: 1px solid {BORDER};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(4)

        self._class_icon = QLabel()
        self._class_icon.setFixedSize(_ICON_CLASS, _ICON_CLASS)
        self._class_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._default_class_icon = _placeholder(_ICON_CLASS)
        self._class_icon.setPixmap(self._default_class_icon)

        self._char_btn = QToolButton()
        self._char_btn.setText("No character loaded  \u25be")
        self._char_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._char_btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._char_btn.setStyleSheet(
            f"QToolButton {{"
            f"  background: transparent;"
            f"  border: none;"
            f"  padding: 0 4px;"
            f"  color: {SUBTEXT0};"
            f"}}"
            f"QToolButton:hover {{ color: {TEXT}; }}"
            "QToolButton::menu-indicator { image: none; }"
        )

        self._actions_btn = QToolButton()
        self._actions_btn.setText("Actions  \u25be")
        self._actions_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._actions_btn.setFixedWidth(105)
        self._actions_btn.setStyleSheet(
            f"QToolButton {{ text-align: center; }}"
            f"QToolButton:hover {{ background: {SURFACE2}; border-color: {BLUE}; }}"
            "QToolButton::menu-indicator { image: none; }"
        )

        self._hp_lbl = QLabel("")
        self._hp_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {SUBTEXT0}; padding-left: 8px; padding-right: 4px;"
        )
        self._hp_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._hp_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Divider between HP and mana
        self._resource_sep = QLabel("|")
        self._resource_sep.setStyleSheet(f"color: {SURFACE2}; font-size: 13px; padding: 0;")
        self._resource_sep.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._resource_sep.setVisible(False)

        self._mana_lbl = QLabel("")
        self._mana_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {SUBTEXT0}; padding-left: 2px; padding-right: 4px;"
        )
        self._mana_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._mana_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._mana_lbl.setVisible(False)

        self._exp_sep = QLabel("|")
        self._exp_sep.setStyleSheet(f"color: {SURFACE2}; font-size: 13px; padding: 0;")
        self._exp_sep.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._exp_sep.setVisible(False)

        self._exp_lbl = QLabel("")
        self._exp_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {SUBTEXT0}; padding-left: 2px; padding-right: 4px;"
        )
        self._exp_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._exp_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._exp_lbl.setVisible(False)

        lay.addWidget(
            self._class_icon,
            0,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        lay.addWidget(self._char_btn, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        lay.addWidget(self._actions_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._hp_lbl, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._resource_sep, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._mana_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._exp_sep, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._exp_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

    def update_from(self, char: dict[str, str], char_name: str = "") -> None:
        cls   = char.get("Class", "")
        race  = char.get("Race", "")
        level = char.get("Level / Exp", "").split(" ")[0]
        mode  = char.get("Mode", "")

        # Class icon
        if cls:
            icon_file = CLASS_ICONS / f"{_to_snake(cls)}_32_bg.png"
            self._default_class_icon = _load_pixmap(icon_file, _ICON_CLASS)
        else:
            self._default_class_icon = _placeholder(_ICON_CLASS)
        self._class_icon.setPixmap(self._default_class_icon)

        parts = [p for p in [char_name or cls, race, f"Level {level}" if level else "", mode] if p]
        label = "   ·   ".join(parts) if parts else "No character loaded"
        self._char_btn.setText(f"{label}  \u25be")

    _HP_STYLE   = "font-size: 13px; font-weight: 700; padding-left: 8px; padding-right: 4px;"
    _MANA_STYLE = "font-size: 13px; font-weight: 700; padding-left: 2px; padding-right: 4px;"

    def set_hp(self, life: float, max_life: float) -> None:
        pct = life / max_life if max_life > 0 else 0
        color = GREEN if pct > 0.5 else ("#f9e2af" if pct > 0.25 else RED)
        self._hp_lbl.setStyleSheet(f"{self._HP_STYLE} color: {color};")
        self._hp_lbl.setText(f"HP  {life:.0f} / {max_life:.0f}")

    def clear_hp(self) -> None:
        self._hp_lbl.setStyleSheet(f"{self._HP_STYLE} color: {SUBTEXT0};")
        self._hp_lbl.setText("")
        self.clear_mana()

    def set_mana(self, mana: float, max_mana: float) -> None:
        self._resource_sep.setVisible(True)
        self._mana_lbl.setVisible(True)
        self._mana_lbl.setStyleSheet(f"{self._MANA_STYLE} color: {BLUE};")
        self._mana_lbl.setText(f"Mana  {mana:.0f} / {max_mana:.0f}")

    def clear_mana(self) -> None:
        self._resource_sep.setVisible(False)
        self._mana_lbl.setVisible(False)
        self._mana_lbl.setText("")

    _EXP_STYLE = "font-size: 13px; font-weight: 700; padding-left: 2px; padding-right: 4px;"

    def set_exp(self, exp: float, needed: float) -> None:
        pct = int(exp / needed * 100) if needed > 0 else 0
        self._exp_sep.setVisible(True)
        self._exp_lbl.setVisible(True)
        self._exp_lbl.setStyleSheet(f"{self._EXP_STYLE} color: {YELLOW};")
        self._exp_lbl.setText(f"EXP  {pct}%")
        self._exp_lbl.setToolTip(f"{exp:.0f} / {needed:.0f} XP to next level")

    def clear_exp(self) -> None:
        self._exp_sep.setVisible(False)
        self._exp_lbl.setVisible(False)
        self._exp_lbl.setText("")

    def set_character_menu(self, menu) -> None:
        self._char_btn.setMenu(menu)

    def set_actions_menu(self, menu) -> None:
        self._actions_btn.setMenu(menu)


def _divider() -> QFrame:
    div = QFrame()
    div.setFrameShape(QFrame.Shape.HLine)
    div.setStyleSheet(f"color: {BORDER}; margin: 4px 0;")
    return div
