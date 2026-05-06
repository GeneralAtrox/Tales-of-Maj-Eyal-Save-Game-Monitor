from __future__ import annotations

import re
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any

from PySide6.QtCore import QSize, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from game_data.talent_db import lookup_talent_description, lookup_talent_icon
from gui.sprite_composer import compose_layers, get_sprite, normalize_sprite_layers
from gui.startup_trace import mark_startup_phase
from gui.theme import (
    BG,
    BLUE,
    BORDER,
    GREEN,
    MAUVE,
    OVERLAY,
    RED,
    SUBTEXT0,
    SUBTEXT1,
    SURFACE0,
    SURFACE1,
    SURFACE2,
    TEAL,
    TEXT,
    YELLOW,
)

# ── Asset paths ───────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
TALENT_ICONS = _ROOT / "Icons" / "talents"
STAT_ICONS = _ROOT / "Icons" / "stats"
CLASS_ICONS = _ROOT / "Icons" / "class-icons"

# ── Sizes ─────────────────────────────────────────────────────────────────────
_ICON_GRID = 44  # talent icon in the grid
_ICON_DETAIL = 64  # talent icon in the detail panel
_ICON_STAT = 28  # stat icon
_ICON_CLASS = 32  # class icon in header
_ICON_SPRITE = 192  # live player sprite beside primary stats
_ICON_ITEM_CARD = 30
_ICON_ITEM_DETAIL = 72

_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}
_STAT_PIXMAP_CACHE: dict[tuple[str, int], QPixmap] = {}
_PLACEHOLDER_CACHE: dict[tuple[int, str], QPixmap] = {}

# ── Stat icon filename mapping ────────────────────────────────────────────────
_STAT_ICONS: dict[str, str] = {
    "Strength": "strength",
    "Dexterity": "dexterity",
    "Constitution": "constitution",
    "Magic": "magic",
    "Willpower": "willpower",
    "Cunning": "cunning",
}
_STAT_ORDER = list(_STAT_ICONS)

# ── Icon overrides ────────────────────────────────────────────────────────────
# Talent names whose icon filename doesn't match their snake_case name.
# Value is the stem (no .png) of the file in Icons/talents/.
_ICON_OVERRIDES: dict[str, str] = {
    "Pulverising Auger": "dig",
    "Pulverizing Auger": "dig",
    "Mirror Image": "mirror_images",
    "Temporal Shield": "time_shield",
    "Arcane Reconstruction": "heal",
    "Ogric Wrath": "ogre_wrath",
    "Heavy Armour Training": "armour_training",
    "Combat Accuracy": "weapon_combat",
    "Dagger Mastery": "knife_mastery",
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_snake(name: str) -> str:
    """'Stunning Blow' → 'stunning_blow', "Hunter's Sight" → 'hunters_sight'."""
    s = name.lower().replace("'", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _normalize_talent_icon_name(icon_value: str) -> str:
    icon_value = icon_value.strip()
    if not icon_value:
        return ""
    return PurePosixPath(icon_value).name


def _resolve_talent_icon_path(name: str, data: Any) -> Path:
    if isinstance(data, dict):
        icon_value = data.get("Icon")
        if isinstance(icon_value, str):
            icon_name = _normalize_talent_icon_name(icon_value)
            if icon_name:
                candidate = TALENT_ICONS / icon_name
                if candidate.exists():
                    return candidate

    fallback_icon = lookup_talent_icon(name)
    if fallback_icon:
        candidate = TALENT_ICONS / fallback_icon
        if candidate.exists():
            return candidate

    return TALENT_ICONS / f"{_ICON_OVERRIDES.get(name, _to_snake(name))}.png"


def _load_pixmap(path: Path, size: int) -> QPixmap:
    cache_key = (str(path), size)
    if cache_key in _PIXMAP_CACHE:
        return _PIXMAP_CACHE[cache_key]
    if path.exists():
        px = QPixmap(str(path))
        if not px.isNull():
            scaled = px.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            _PIXMAP_CACHE[cache_key] = scaled
            return scaled
    placeholder = _placeholder(size)
    _PIXMAP_CACHE[cache_key] = placeholder
    return placeholder


def _load_stat_pixmap(path: Path, size: int) -> QPixmap:
    """Load stat icons with black matte pixels converted to transparency."""
    cache_key = (str(path), size)
    if cache_key in _STAT_PIXMAP_CACHE:
        return _STAT_PIXMAP_CACHE[cache_key]
    if not path.exists():
        placeholder = _placeholder(size)
        _STAT_PIXMAP_CACHE[cache_key] = placeholder
        return placeholder

    image = QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32)
    if image.isNull():
        placeholder = _placeholder(size)
        _STAT_PIXMAP_CACHE[cache_key] = placeholder
        return placeholder

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
        return color.alpha() > 0 and color.red() <= 40 and color.green() <= 40 and color.blue() <= 40

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
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    _STAT_PIXMAP_CACHE[cache_key] = scaled
    return scaled


def _placeholder(size: int, letter: str = "?") -> QPixmap:
    cache_key = (size, letter[:1].upper())
    if cache_key in _PLACEHOLDER_CACHE:
        return _PLACEHOLDER_CACHE[cache_key]
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
    _PLACEHOLDER_CACHE[cache_key] = px
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

    clicked = Signal(str, object)  # talent_name, data

    def __init__(
        self,
        name: str,
        data: Any,
        parent: QWidget | None = None,
        *,
        show_level: bool = True,
        icon_size: int = _ICON_GRID,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._data = data

        level = _level_of(data)
        zero = _is_zero_level(level)

        self.setFixedSize(icon_size + 8, icon_size + (20 if show_level else 8))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{name}  [{level}]")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        # Icon
        px = _load_pixmap(_resolve_talent_icon_path(name, data), icon_size)
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
        icon_lbl.setFixedSize(icon_size, icon_size)
        icon_lbl.setPixmap(px)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Level text
        level_lbl = QLabel(level)
        level_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        level_color = SUBTEXT0 if zero else GREEN
        level_lbl.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {level_color};")

        lay.addWidget(icon_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        if show_level:
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
        self.setObjectName("TalentDetailPanel")
        self.setStyleSheet(
            "QWidget#TalentDetailPanel, QWidget#TalentDetailFields {"
            " background: transparent; border: none; }"
            "QWidget#TalentDetailFields QLabel {"
            " background: transparent; border: none; }"
        )

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
        self._name_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        self._name_lbl.setWordWrap(True)
        self._name_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._name_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._category_lbl = QLabel()
        self._category_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
        self._category_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._category_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._category_lbl.hide()
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
        self._fields_w.setObjectName("TalentDetailFields")
        self._fields_lay = QGridLayout(self._fields_w)
        self._fields_lay.setContentsMargins(0, 4, 0, 0)
        self._fields_lay.setHorizontalSpacing(10)
        self._fields_lay.setVerticalSpacing(5)
        self._fields_lay.setColumnMinimumWidth(0, 95)
        self._fields_lay.setColumnStretch(0, 0)
        self._fields_lay.setColumnStretch(1, 1)
        self._field_row = 0
        root.addWidget(self._fields_w)
        root.addStretch()

        self._show_placeholder()

    # ── Field color map ───────────────────────────────────────────────────
    _COLORS: dict[str, str] = {
        "Level": GREEN,
        "Effective Talent Level": GREEN,
        "Mode": SUBTEXT0,
        "Status": YELLOW,
        "Type": BLUE,
        "Range": BLUE,
        "Cooldown": YELLOW,
        "Travel Speed": SUBTEXT1,
        "Usage Speed": SUBTEXT1,
        "Scales With": MAUVE,
        "Turn Duration": GREEN,
        "Charges": TEXT,
        "Stacks": TEXT,
        "Power": TEXT,
        "Source": SUBTEXT0,
        "Stats": TEXT,
        "Stats per turn": TEXT,
        "Remaining Requirements": YELLOW,
        "Description": SUBTEXT1,
    }
    _ORDER = [
        "Level",
        "Effective Talent Level",
        "Mode",
        "Status",
        "Type",
        "Range",
        "Cooldown",
        "Travel Speed",
        "Usage Speed",
        "Scales With",
        "Turn Duration",
        "Charges",
        "Stacks",
        "Power",
        "Source",
        "Stats",
        "Stats per turn",
        "Remaining Requirements",
        "Description",
    ]

    def show_talent(self, name: str, data: Any, category: str = "") -> None:
        self._clear_fields()

        px = _load_pixmap(_resolve_talent_icon_path(name, data), _ICON_DETAIL)
        self._icon_lbl.setPixmap(px)
        self._name_lbl.setText(name)
        self._category_lbl.setText(category)
        self._category_lbl.setVisible(bool(category))

        if isinstance(data, dict) and "Description" not in data:
            fallback_desc = lookup_talent_description(name)
            if fallback_desc:
                data = dict(data)
                data["Description"] = fallback_desc

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
        row = self._field_row
        self._field_row += 1
        lbl = QLabel(f"{label}")
        lbl.setMinimumWidth(95)
        lbl.setStyleSheet(f"color: {SUBTEXT0}; font-size: 12px; background: transparent; border: none;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        val = QLabel(value)
        val.setStyleSheet(f"color: {color}; font-size: 12px; background: transparent; border: none;")
        val.setWordWrap(True)
        val.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._fields_lay.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._fields_lay.addWidget(val, row, 1, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    def _clear_fields(self) -> None:
        while self._fields_lay.count():
            item = self._fields_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._field_row = 0

    def _show_placeholder(self) -> None:
        self._icon_lbl.setPixmap(_placeholder(_ICON_DETAIL, "?"))
        self._name_lbl.setText("Select a talent")
        self._category_lbl.setText("")
        self._category_lbl.hide()


class _CategoryHeader(QLabel):
    """Inline category separator inside a talent section."""

    def __init__(self, name: str, mastery: str, parent: QWidget | None = None) -> None:
        display = f"  {name}  (×{mastery})"
        super().__init__(display, parent)
        self.setStyleSheet(
            f"color: {YELLOW}; font-size: 11px; font-weight: 600; padding: 4px 0 2px 0; letter-spacing: 0.3px;"
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
            val_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT};")

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


class _CompactTalentStrip(QWidget):
    """Compact icon-only strip for always-visible live talents such as sustains."""

    clicked = Signal(str, object, str)  # talent_name, data, category

    def __init__(
        self,
        talents: dict[str, Any],
        *,
        category: str,
        left_indent: int = 4,
        icon_size: int = 28,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(left_indent, 2, 4, 0)
        root.setSpacing(0)

        icons_host = QWidget()
        icons_lay = QGridLayout(icons_host)
        icons_lay.setContentsMargins(0, 0, 0, 0)
        icons_lay.setHorizontalSpacing(4)
        icons_lay.setVerticalSpacing(4)

        columns = 12
        for index, (talent_name, talent_data) in enumerate(talents.items()):
            icon_w = _TalentIcon(talent_name, talent_data, show_level=False, icon_size=icon_size)
            icon_w.clicked.connect(
                lambda name, data, cat=category: self.clicked.emit(name, data, cat)
            )
            icons_lay.addWidget(icon_w, index // columns, index % columns)

        root.addWidget(icons_host)


class _PlayerOverview(QWidget):
    """Sprite, primary stats, and live sustain/effect strips in one aligned block."""

    clicked = Signal(str, object, str)  # talent_name, data, category

    def __init__(
        self,
        stats: dict[str, str],
        *,
        sprite: QPixmap | None = None,
        sustains: dict[str, Any] | None = None,
        effects: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        root = QHBoxLayout(self)
        root.setContentsMargins(4, 0, 4, 4)
        root.setSpacing(12)

        if sprite is not None:
            sprite_lbl = QLabel()
            sprite_lbl.setPixmap(sprite)
            sprite_lbl.setFixedSize(sprite.size())
            sprite_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            root.addWidget(sprite_lbl, 0, Qt.AlignmentFlag.AlignTop)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(2)

        right_col.addWidget(_StatsRow(stats), 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        for entries in (sustains, effects):
            if not entries:
                continue
            strip = _CompactTalentStrip(entries, category="", left_indent=0, icon_size=24)
            strip.clicked.connect(self.clicked)
            right_col.addWidget(strip, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        right_col.addStretch()
        root.addLayout(right_col, 1)


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
    "Ring": 10,
    "Left ring": 10,
    "Right ring": 11,
    "Lite": 12,
    "Tool": 13,
}

_ITEM_PROPERTY_ORDER = [
    "Base power",
    "Uses stat",
    "Uses stats",
    "Damage type",
    "Accuracy bonus",
    "Accuracy",
    "Armour penetration",
    "Armour Penetration",
    "Critical multiplier",
    "Crit. chance",
    "Physical crit. chance",
    "Mental crit. chance",
    "Spell crit. chance",
    "Attack speed",
    "Travel speed",
    "Defense",
    "Ranged Defense",
    "Armour",
    "Block",
    "On shield block",
    "Capacity",
    "Power source",
    "Activates",
    "When used",
    "Description",
]

_ITEM_NAME_PREFIXES = (
    "when you leave the level.",
    "when you enter the level.",
)
_ITEM_MATERIAL_FAMILIES = {
    "jewelry": ("copper", "steel", "gold", "stralite", "voratun"),
    "metal": ("iron", "steel", "dsteel", "stralite", "voratun"),
    "leather": ("rough", "cured", "hardened", "reinforced", "drakeskin"),
    "cloth": ("linen", "woollen", "cashmere", "silk", "elvensilk"),
    "wood": ("elm", "ash", "yew", "elvenwood", "dragonbone"),
    "nature": ("mossy", "vined", "thorned", "pulsing", "living"),
    "lite": ("brass", "", "dwarven", "", "faenorian"),
}
_ITEM_ICON_RULES = {
    ("jewelry", "ring"): ("ring", "jewelry"),
    ("jewelry", "amulet"): ("amulet", "jewelry"),
    ("armor", "belt"): ("belt", "leather"),
    ("armor", "cloth"): ("robe", "cloth"),
    ("armor", "light"): ("leather", "leather"),
    ("armor", "heavy"): ("mail", "metal"),
    ("armor", "massive"): ("plate", "metal"),
    ("armor", "cloak"): ("cloak", "cloth"),
    ("armor", "shield"): ("shield", "metal"),
    ("weapon", "staff"): ("staff", "wood"),
    ("weapon", "dagger"): ("knife", "metal"),
    ("weapon", "knife"): ("knife", "metal"),
    ("weapon", "sword"): ("sword", "metal"),
    ("weapon", "axe"): ("axe", "metal"),
    ("weapon", "waraxe"): ("axe", "metal"),
    ("weapon", "mace"): ("mace", "metal"),
    ("weapon", "greatmace"): ("2hmace", "metal"),
    ("weapon", "bow"): ("longbow", "wood"),
    ("weapon", "longbow"): ("longbow", "wood"),
    ("weapon", "sling"): ("sling", "leather"),
    ("weapon", "mindstar"): ("mindstar", "nature"),
    ("charm", "wand"): ("wand", "wood"),
    ("charm", "torque"): ("torque", "metal"),
    ("lite", "lite"): ("lite", "lite"),
    ("ammo", "shot"): ("shot", "metal"),
}
_ITEM_MODDABLE_TILE_RULES = {
    "robe": ("robe", "cloth"),
    "light": ("leather", "leather"),
    "heavy": ("mail", "metal"),
    "massive": ("plate", "metal"),
    "cloak": ("cloak", "cloth"),
    "shield": ("shield", "metal"),
    "staff": ("staff", "wood"),
    "sword": ("sword", "metal"),
    "dagger": ("knife", "metal"),
    "axe": ("axe", "metal"),
    "mace": ("mace", "metal"),
    "2hmace": ("2hmace", "metal"),
    "bow": ("longbow", "wood"),
    "sling": ("sling", "leather"),
    "mindstar": ("mindstar", "nature"),
    "wizard_hat": ("wizardhat", "cloth"),
    "helm": ("helm", "metal"),
    "leather_cap": ("cap", "leather"),
    "gauntlets": ("hgloves", "metal"),
    "gloves": ("gloves", "leather"),
    "leather_boots": ("boots", "leather"),
    "heavy_boots": ("hboots", "metal"),
}
_ITEM_ARTIFACT_ICON_OVERRIDES = {
    "rogue plight": "object/artifact/armor_rogue_plight.png",
    "blood-letter": "object/artifact/weapon_axe_blood_letter.png",
}
_ITEM_STAT_LABELS = {
    "Strength": "STR",
    "Dexterity": "DEX",
    "Constitution": "CON",
    "Magic": "MAG",
    "Willpower": "WIL",
    "Cunning": "CUN",
    "Str": "STR",
    "Dex": "DEX",
    "Con": "CON",
    "Mag": "MAG",
    "Wil": "WIL",
    "Cun": "CUN",
    "Physical power": "PHYS",
    "Spellpower": "SPELL",
    "Mindpower": "MIND",
    "Accuracy": "ACC",
    "Defense": "DEF",
    "Ranged Defense": "RDEF",
    "Armour": "ARM",
    "Armour penetration": "APR",
    "Armour Penetration": "APR",
    "Crit. chance": "CRIT",
    "Physical crit. chance": "PCRIT",
    "Mental crit. chance": "MCRIT",
    "Spell crit. chance": "SCRIT",
    "Maximum life": "LIFE",
    "Maximum mana": "MANA",
    "Maximum stamina": "STAM",
    "Maximum vim": "VIM",
    "Life regen": "REGEN",
    "Mana each turn": "MANA/T",
    "Stamina each turn": "STAM/T",
}
_ITEM_STAT_PRIORITY = (
    "Changes stats",
    "Physical power",
    "Spellpower",
    "Mindpower",
    "Accuracy",
    "Defense",
    "Ranged Defense",
    "Armour",
    "Armour penetration",
    "Armour Penetration",
    "Crit. chance",
    "Physical crit. chance",
    "Mental crit. chance",
    "Spell crit. chance",
    "Maximum life",
    "Maximum mana",
    "Maximum stamina",
    "Maximum vim",
    "Life regen",
    "Mana each turn",
    "Stamina each turn",
)

_ITEM_SLOT_ALIASES = {
    "On feet": "Feet",
    "On hands": "Hands",
    "On head": "Head",
    "Around neck": "Neck",
    "On fingers": "Ring",
    "Light source": "Lite",
    "On body": "Body",
    "In main hand": "Mainhand",
    "In offhand": "Offhand",
    "Main hand": "Mainhand",
    "Off hand": "Offhand",
    "In quiver": "Quiver",
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


def _clean_item_name_text(name: str) -> str:
    cleaned = re.sub(r"^\d+\s+", "", name).strip()
    lowered = cleaned.lower()
    for prefix in _ITEM_NAME_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            lowered = cleaned.lower()
    while True:
        updated = re.sub(r"(?:\s+\[[^\]]+\]|\s+\([^()]*\))+$", "", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned or name.strip()


def _label_selectable(label: QLabel) -> None:
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )


def _display_item_name(item: dict[str, Any]) -> str:
    name = str(item.get("Name") or "Unknown Item")
    return _clean_item_name_text(name)


def _normalize_item_slot(slot: str) -> str:
    slot = " ".join(slot.split()).strip()
    return _ITEM_SLOT_ALIASES.get(slot, slot)


def _normalize_item_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = " ".join(str(raw_tag).split()).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    return normalized


def _parse_inventory_dump_item(text: str) -> dict[str, Any]:
    compact = " ".join(text.split()).strip()
    if not compact:
        return {"Name": "Unknown Item"}

    segments = [segment.strip() for segment in compact.split(" | ") if segment.strip()]
    if not segments:
        return {"Name": compact}

    entry: dict[str, Any] = {"Name": segments[0]}
    tags: list[str] = []
    properties: dict[str, str] = {}
    description_parts: list[str] = []

    for segment in segments[1:]:
        for tag in re.findall(r"\[([^\]]+)\]", segment):
            clean_tag = " ".join(tag.split()).strip()
            if clean_tag:
                tags.append(clean_tag)

        clean_segment = re.sub(r"\s*\[[^\]]+\]", "", segment).strip()
        tier_match = re.fullmatch(r"tier\s+(\d+)", clean_segment, flags=re.IGNORECASE)
        if tier_match:
            entry["Tier"] = int(tier_match.group(1))
            continue

        if ":" in clean_segment:
            key, value = clean_segment.split(":", 1)
            key = " ".join(key.split()).strip()
            value = " ".join(value.split()).strip()
            if key and value:
                properties[key] = value
            continue

        if clean_segment:
            description_parts.append(clean_segment)

    normalized_tags = _normalize_item_tags(tags)
    if normalized_tags:
        entry["Tags"] = normalized_tags
    if description_parts:
        properties["Description"] = " ".join(description_parts)
    if properties:
        entry["Properties"] = properties
    return entry


def _normalize_item_record(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return _parse_inventory_dump_item(item)
    if not isinstance(item, dict):
        return None

    entry = dict(item)
    name = " ".join(str(entry.get("Name") or "Unknown Item").split()).strip()
    entry["Name"] = name or "Unknown Item"

    slot = entry.get("Slot")
    if isinstance(slot, str) and slot.strip():
        entry["Slot"] = _normalize_item_slot(slot)

    type_value = entry.get("Type")
    if isinstance(type_value, str):
        clean_type = " ".join(type_value.split()).strip()
        if " / " in clean_type and not entry.get("Subtype"):
            base_type, subtype = clean_type.split(" / ", 1)
            entry["Type"] = base_type
            entry["Subtype"] = subtype
        else:
            entry["Type"] = clean_type

    subtype = entry.get("Subtype")
    if isinstance(subtype, str):
        entry["Subtype"] = " ".join(subtype.split()).strip()

    normalized_tags = _normalize_item_tags(entry.get("Tags"))
    if normalized_tags:
        entry["Tags"] = normalized_tags
    elif "Tags" in entry:
        entry.pop("Tags", None)
    identified = any(tag.lower() == "identified" for tag in normalized_tags)

    props = entry.get("Properties")
    normalized_props: dict[str, str] = {}
    if isinstance(props, dict):
        for raw_key, raw_value in props.items():
            key = " ".join(str(raw_key).split()).strip()
            if not key:
                continue
            if isinstance(raw_value, list):
                value = ", ".join(" ".join(str(part).split()).strip() for part in raw_value if str(part).strip())
            else:
                value = " ".join(str(raw_value).split()).strip()
            if value:
                normalized_props[key] = value

    description = entry.get("Description")
    if isinstance(description, str) and description.strip() and "Description" not in normalized_props:
        normalized_props["Description"] = " ".join(description.split()).strip()
    if identified:
        normalized_props.pop("Unidentified name", None)

    if normalized_props:
        entry["Properties"] = normalized_props
    elif "Properties" in entry:
        entry.pop("Properties", None)

    icon = entry.get("Icon")
    if isinstance(icon, str):
        clean_icon = " ".join(icon.split()).strip()
        if clean_icon.endswith(".png"):
            entry["Icon"] = clean_icon
        elif "Icon" in entry:
            entry.pop("Icon", None)
    elif "Icon" in entry:
        entry.pop("Icon", None)

    return entry


def _display_item_term(value: str) -> str:
    clean = " ".join(value.split()).strip()
    if not clean:
        return ""
    parts = re.split(r"([ -])", clean)
    return "".join(part[:1].upper() + part[1:] if part not in {" ", "-"} else part for part in parts)


def _item_kind_label(item: dict[str, Any]) -> str:
    item_type = _display_item_term(str(item.get("Type") or ""))
    subtype = _display_item_term(str(item.get("Subtype") or ""))
    if item_type and subtype:
        return f"{item_type} / {subtype}"
    return item_type or subtype


def _item_description(item: dict[str, Any]) -> str:
    description = _item_property(item, "Description")
    return description or ""


def _item_icon_hint(item: dict[str, Any]) -> str:
    icon = item.get("Icon")
    if not isinstance(icon, str):
        return ""
    return icon.strip()


def _resolve_existing_icon_hint(candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate and get_sprite(candidate) is not None:
            return candidate
    return ""


def _item_material_level(item: dict[str, Any]) -> int | None:
    for key in ("MaterialLevel", "Tier"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return max(1, int(value))
    return None


def _resolve_material_name(family: str, level: int | None) -> str:
    materials = _ITEM_MATERIAL_FAMILIES.get(family)
    if not materials or level is None:
        return ""
    index = min(max(level, 1), len(materials)) - 1
    return materials[index]


def _item_name_candidates(item: dict[str, Any]) -> list[str]:
    candidates = [
        _display_item_name(item).lower(),
        str(_item_property(item, "Unidentified name") or "").lower(),
        str(item.get("Name") or "").lower(),
        str(item.get("ShortName") or "").lower(),
    ]
    return [candidate for candidate in candidates if candidate]


def _resolve_material_icon_hint(item: dict[str, Any]) -> str:
    def candidate_icon(stem: str, family: str) -> str:
        material = _resolve_material_name(family, level)
        if not material:
            return ""
        return _resolve_existing_icon_hint([f"object/{stem}_{material}.png"])

    level = _item_material_level(item)
    if level is None:
        return ""

    moddable_tile = str(item.get("ModdableTile") or "").strip().lower()
    if moddable_tile:
        rule = _ITEM_MODDABLE_TILE_RULES.get(moddable_tile)
        if rule:
            stem, family = rule
            if icon_hint := candidate_icon(stem, family):
                return icon_hint

    item_type = str(item.get("Type") or "").strip().lower()
    subtype = str(item.get("Subtype") or "").strip().lower()
    if rule := _ITEM_ICON_RULES.get((item_type, subtype)):
        stem, family = rule
        if icon_hint := candidate_icon(stem, family):
            return icon_hint

    name_candidates = _item_name_candidates(item)
    if item_type == "armor" and subtype == "head":
        if any("wizard hat" in name for name in name_candidates):
            if icon_hint := candidate_icon("wizardhat", "cloth"):
                return icon_hint
        if any("cap" in name for name in name_candidates):
            if icon_hint := candidate_icon("cap", "leather"):
                return icon_hint
        if icon_hint := candidate_icon("helm", "metal"):
            return icon_hint

    if item_type == "armor" and subtype == "hands":
        if any("gauntlet" in name for name in name_candidates):
            if icon_hint := candidate_icon("hgloves", "metal"):
                return icon_hint
        if icon_hint := candidate_icon("gloves", "leather"):
            return icon_hint

    if item_type == "armor" and subtype == "feet":
        heavy_boot_markers = ("mail boots", "iron boots", "steel boots", "stralite boots", "voratun boots")
        if any(any(marker in name for marker in heavy_boot_markers) for name in name_candidates):
            if icon_hint := candidate_icon("hboots", "metal"):
                return icon_hint
        if icon_hint := candidate_icon("boots", "leather"):
            return icon_hint

    return ""


def _guess_item_icon_hint_from_name(item: dict[str, Any]) -> str:
    clean_name_candidates = [_clean_item_name_text(candidate).lower() for candidate in _item_name_candidates(item)]

    def find_material(noun: str, family: str) -> str:
        materials = _ITEM_MATERIAL_FAMILIES.get(family, ())
        for name in clean_name_candidates:
            for material in materials:
                if material and re.search(rf"\b{re.escape(material)}\s+{re.escape(noun)}\b", name):
                    return material
        return ""

    if any(" ring" in name for name in clean_name_candidates):
        if material := find_material("ring", "jewelry"):
            return _resolve_existing_icon_hint([f"object/ring_{material}.png"])

    if any(" amulet" in name for name in clean_name_candidates):
        if material := find_material("amulet", "jewelry"):
            return _resolve_existing_icon_hint([f"object/amulet_{material}.png"])

    if any(" belt" in name for name in clean_name_candidates):
        if material := find_material("belt", "leather"):
            return _resolve_existing_icon_hint([f"object/belt_{material}.png"])

    if any(" robe" in name for name in clean_name_candidates):
        if material := find_material("robe", "cloth"):
            return _resolve_existing_icon_hint([f"object/robe_{material}.png"])

    if any("wizard hat" in name for name in clean_name_candidates):
        if material := find_material("wizard hat", "cloth"):
            return _resolve_existing_icon_hint([f"object/wizardhat_{material}.png"])

    if any("gauntlet" in name for name in clean_name_candidates):
        if material := find_material("gauntlets", "metal"):
            return _resolve_existing_icon_hint([f"object/hgloves_{material}.png"])

    if any("glove" in name for name in clean_name_candidates):
        if material := find_material("gloves", "leather"):
            return _resolve_existing_icon_hint([f"object/gloves_{material}.png"])

    return ""


def _guess_item_icon_hint(item: dict[str, Any]) -> str:
    icon_hint = _item_icon_hint(item)
    if icon_hint:
        return icon_hint

    name_candidates = _item_name_candidates(item)
    clean_name_candidates = [_clean_item_name_text(candidate).lower() for candidate in name_candidates]

    for name in clean_name_candidates:
        override = _ITEM_ARTIFACT_ICON_OVERRIDES.get(name)
        if override:
            return override

    artifact_stems = [_to_snake(name) for name in clean_name_candidates if name]
    if artifact_hint := _resolve_existing_icon_hint([f"object/artifact/{stem}.png" for stem in artifact_stems]):
        return artifact_hint

    if material_hint := _resolve_material_icon_hint(item):
        return material_hint

    if fallback_hint := _guess_item_icon_hint_from_name(item):
        return fallback_hint

    return ""


def _item_icon_pixmap(item: dict[str, Any], size: int) -> QPixmap:
    icon_hint = _guess_item_icon_hint(item)
    if icon_hint:
        sprite_path = get_sprite(icon_hint)
        if sprite_path is not None:
            return _load_pixmap(sprite_path, size)
    fallback_letter = _display_item_name(item)[:1] or str(item.get("Type") or "?")[:1] or "?"
    return _placeholder(size, fallback_letter)


def _item_list_icon_pixmap(item: dict[str, Any], size: int) -> QPixmap:
    icon_hint = _item_icon_hint(item)
    if icon_hint:
        local_path = _ROOT / "Icons" / icon_hint
        if local_path.exists():
            return _load_pixmap(local_path, size)

    for candidate in _item_name_candidates(item):
        override = _ITEM_ARTIFACT_ICON_OVERRIDES.get(_clean_item_name_text(candidate).lower())
        if not override:
            continue
        local_path = _ROOT / "Icons" / override
        if local_path.exists():
            return _load_pixmap(local_path, size)

    fallback_letter = _display_item_name(item)[:1] or str(item.get("Type") or "?")[:1] or "?"
    return _placeholder(size, fallback_letter)


def _item_preview_text(item: dict[str, Any]) -> str:
    summary = _item_summary_fields(item)
    if summary:
        return "  |  ".join(summary)
    description = _item_description(item)
    if not description:
        return ""
    return description if len(description) <= 160 else f"{description[:157].rstrip()}..."


def _ordered_item_properties(item: dict[str, Any]) -> list[tuple[str, str]]:
    props = item.get("Properties")
    if not isinstance(props, dict):
        return []

    priority = {key: index for index, key in enumerate(_ITEM_PROPERTY_ORDER)}
    rows: list[tuple[str, str]] = []
    for raw_key, raw_value in props.items():
        key = " ".join(str(raw_key).split()).strip()
        if not key:
            continue
        if isinstance(raw_value, list):
            value = ", ".join(str(part) for part in raw_value)
        else:
            value = " ".join(str(raw_value).split()).strip()
        if not value:
            continue
        rows.append((key, value))

    rows.sort(key=lambda item_row: (priority.get(item_row[0], len(priority)), item_row[0].lower()))
    return rows


def _inventory_item_key(item: dict[str, Any]) -> tuple[Any, ...]:
    properties = tuple(_ordered_item_properties(item)[:8])
    return (
        _display_item_name(item).lower(),
        str(item.get("Slot") or "").lower(),
        str(item.get("Type") or "").lower(),
        str(item.get("Subtype") or "").lower(),
        item.get("Tier"),
        tuple(str(tag).lower() for tag in item.get("Tags", [])),
        properties,
    )


def _items_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _display_item_name(left).lower() != _display_item_name(right).lower():
        return False
    for field in ("Slot", "Type", "Subtype"):
        left_value = str(left.get(field) or "").strip().lower()
        right_value = str(right.get(field) or "").strip().lower()
        if left_value and right_value and left_value != right_value:
            return False
    for field in ("Tier", "Count"):
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def _merge_item_record(live_item: dict[str, Any], file_item: dict[str, Any] | None) -> dict[str, Any]:
    if file_item is None:
        return dict(live_item)

    merged = dict(file_item)
    merged.update(live_item)

    merged_tags = _normalize_item_tags(
        [*list(file_item.get("Tags") or []), *list(live_item.get("Tags") or [])]
    )
    if merged_tags:
        merged["Tags"] = merged_tags
    else:
        merged.pop("Tags", None)

    properties: dict[str, str] = {}
    for source in (file_item.get("Properties"), live_item.get("Properties")):
        if not isinstance(source, dict):
            continue
        for raw_key, raw_value in source.items():
            key = " ".join(str(raw_key).split()).strip()
            value = " ".join(str(raw_value).split()).strip()
            if key and value:
                properties[key] = value

    if any(tag.lower() == "identified" for tag in merged_tags):
        properties.pop("Unidentified name", None)
    if properties:
        merged["Properties"] = properties
    else:
        merged.pop("Properties", None)

    return _normalize_item_record(merged) or dict(merged)


def _merge_item_sources(live_items: list[Any] | None, file_items: list[Any] | None) -> list[dict[str, Any]]:
    normalized_live = [_normalize_item_record(item) for item in (live_items or [])]
    normalized_file = [_normalize_item_record(item) for item in (file_items or [])]
    live_records = [item for item in normalized_live if item is not None]
    file_records = [item for item in normalized_file if item is not None]

    if not live_records:
        return list(file_records)
    if not file_records:
        return list(live_records)

    merged: list[dict[str, Any]] = []
    used_file_indexes: set[int] = set()
    for live_item in live_records:
        matched_file: dict[str, Any] | None = None
        for index, file_item in enumerate(file_records):
            if index in used_file_indexes:
                continue
            if _items_match(live_item, file_item):
                matched_file = file_item
                used_file_indexes.add(index)
                break
        merged.append(_merge_item_record(live_item, matched_file))

    for index, file_item in enumerate(file_records):
        if index not in used_file_indexes:
            merged.append(dict(file_item))
    return merged


def _parse_changes_stats(value: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for part in value.split("/"):
        clean = " ".join(part.split()).strip()
        if not clean:
            continue
        match = re.match(r"([+-]?\d+(?:\.\d+)?%?)\s+([A-Za-z]+)$", clean)
        if not match:
            continue
        raw_value, raw_stat = match.groups()
        label = _ITEM_STAT_LABELS.get(raw_stat, raw_stat.upper())
        rows.append((label, raw_value))
    return rows


def _item_stat_highlights(item: dict[str, Any], *, limit: int = 8) -> list[tuple[str, str]]:
    highlights: list[tuple[str, str]] = []
    seen: set[str] = set()

    changes = _item_property(item, "Changes stats")
    if changes:
        for label, value in _parse_changes_stats(changes):
            key = f"{label}|{value}"
            if key in seen:
                continue
            seen.add(key)
            highlights.append((label, value))

    for prop_key in _ITEM_STAT_PRIORITY:
        if prop_key == "Changes stats":
            continue
        value = _item_property(item, prop_key)
        if not value:
            continue
        label = _ITEM_STAT_LABELS.get(prop_key, prop_key.upper())
        key = f"{label}|{value}"
        if key in seen:
            continue
        seen.add(key)
        highlights.append((label, value))
        if len(highlights) >= limit:
            break

    return highlights[:limit]


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
    clicked = Signal(object)

    def __init__(self, item: dict[str, Any], *, show_slot: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self._accent = _item_rank_color(item)
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(_ICON_ITEM_CARD, _ICON_ITEM_CARD)
        icon_lbl.setPixmap(_item_icon_pixmap(item, _ICON_ITEM_CARD))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_row.addWidget(icon_lbl)
        if show_slot and item.get("Slot"):
            slot_lbl = QLabel(str(item["Slot"]))
            slot_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            title_row.addWidget(slot_lbl)

        name_lbl = QLabel(_display_item_name(item))
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {self._accent};")
        title_row.addWidget(name_lbl, 1)

        meta_parts: list[str] = []
        if isinstance(item.get("Tier"), int):
            meta_parts.append(f"Tier {item['Tier']}")
        tags = item.get("Tags")
        if isinstance(tags, list) and tags:
            meta_parts.append(" ".join(f"[{tag}]" for tag in tags))
        if meta_parts:
            meta_lbl = QLabel("  ".join(meta_parts))
            meta_lbl.setWordWrap(True)
            meta_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            title_row.addWidget(meta_lbl)
        lay.addLayout(title_row)

        secondary_parts: list[str] = []
        if not show_slot and item.get("Slot"):
            secondary_parts.append(str(item["Slot"]))
        if kind := _item_kind_label(item):
            secondary_parts.append(kind)
        if secondary_parts:
            secondary_lbl = QLabel("  •  ".join(secondary_parts))
            secondary_lbl.setWordWrap(True)
            secondary_lbl.setStyleSheet(f"font-size: 11px; color: {SUBTEXT0};")
            lay.addWidget(secondary_lbl)

        preview = _item_preview_text(item)
        if preview:
            preview_lbl = QLabel(preview)
            preview_lbl.setWordWrap(True)
            preview_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT};")
            lay.addWidget(preview_lbl)

    def _apply_style(self) -> None:
        background = SURFACE1 if self._selected else SURFACE0
        border = TEAL if self._selected else BORDER
        self.setStyleSheet(
            f"background: {background}; border: 1px solid {border}; border-left: 3px solid {self._accent};"
            f" border-radius: 4px;"
        )

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._apply_style()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.clicked.emit(self._item)
        super().mousePressEvent(event)


class _ItemListPanel(QWidget):
    item_selected = Signal(str, object)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._items: list[dict[str, Any]] = []
        self._rows: list[tuple[dict[str, Any], tuple[Any, ...]]] = []
        self._last_items: list[Any] = []
        self._last_show_slot = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 0, 0)
        root.setSpacing(6)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;")
        root.addWidget(self._title_lbl)

        self._list = QListWidget()
        self._list.setUniformItemSizes(True)
        self._list.setIconSize(QSize(_ICON_ITEM_CARD, _ICON_ITEM_CARD))
        self._list.setSpacing(4)
        self._list.setWordWrap(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setStyleSheet(
            f"QListWidget {{ background: {BG}; border: none; outline: none; }}"
            f"QListWidget::item {{ background: {SURFACE0}; border: 1px solid {BORDER};"
            f" border-radius: 4px; padding: 6px; color: {TEXT}; }}"
            f"QListWidget::item:selected {{ background: {SURFACE1}; border-color: {TEAL}; }}"
        )
        self._list.currentItemChanged.connect(self._handle_current_item_changed)
        root.addWidget(self._list, 1)

    @property
    def title(self) -> str:
        return self._title

    def set_items(self, items: list[Any], *, show_slot: bool) -> list[dict[str, Any]]:
        normalized_items: list[dict[str, Any]] = []
        for raw_item in items:
            if normalized := _normalize_item_record(raw_item):
                normalized_items.append(normalized)

        sort_key = _slot_sort_key if show_slot else _inventory_sort_key
        sorted_items = sorted(normalized_items, key=sort_key)
        if sorted_items == self._items and show_slot == self._last_show_slot:
            return list(self._items)

        self._last_items = list(items)
        self._last_show_slot = show_slot
        self._items = sorted_items
        self._title_lbl.setText(f"{self._title}  ·  {len(self._items)}")

        with QSignalBlocker(self._list):
            self._list.clear()
            self._rows = []

            if not self._items:
                empty = QListWidgetItem("No items")
                empty.setForeground(QColor(SUBTEXT0))
                empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self._list.addItem(empty)
                return []

            for item_data in self._items:
                key = _inventory_item_key(item_data)
                row_index = len(self._rows)
                list_item = QListWidgetItem(
                    QIcon(_item_list_icon_pixmap(item_data, _ICON_ITEM_CARD)),
                    self._item_text(item_data, show_slot),
                )
                list_item.setForeground(QColor(_item_rank_color(item_data)))
                list_item.setSizeHint(QSize(0, 64))
                list_item.setData(Qt.ItemDataRole.UserRole, row_index)
                self._rows.append((item_data, key))
                self._list.addItem(list_item)
        return list(self._items)

    def set_selected_key(self, key: tuple[Any, ...] | None) -> None:
        target_row = -1
        if key is not None:
            for row, (_item, item_key) in enumerate(self._rows):
                if item_key == key:
                    target_row = row
                    break
        with QSignalBlocker(self._list):
            self._list.setCurrentRow(target_row)

    def find_item(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        for item in self._items:
            if _inventory_item_key(item) == key:
                return item
        return None

    def first_item(self) -> dict[str, Any] | None:
        return self._items[0] if self._items else None

    def _handle_current_item_changed(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, int) or row < 0 or row >= len(self._rows):
            return
        self.item_selected.emit(self._title, self._rows[row][0])

    @staticmethod
    def _item_text(item: dict[str, Any], show_slot: bool) -> str:
        lines = [_display_item_name(item)]
        meta_parts: list[str] = []
        if show_slot and item.get("Slot"):
            meta_parts.append(str(item["Slot"]))
        elif not show_slot and item.get("Slot"):
            meta_parts.append(str(item["Slot"]))
        if isinstance(item.get("Tier"), int):
            meta_parts.append(f"Tier {item['Tier']}")
        if kind := _item_kind_label(item):
            meta_parts.append(kind)
        tags = item.get("Tags")
        if isinstance(tags, list) and tags:
            meta_parts.extend(f"[{tag}]" for tag in tags)
        if meta_parts:
            lines.append("  |  ".join(meta_parts))
        preview = _item_preview_text(item)
        if preview:
            lines.append(preview)
        return "\n".join(lines)

    def _handle_card_clicked(self, item: object) -> None:
        self.item_selected.emit(self._title, item)


class _InventoryDetailPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {SURFACE0}; border-left: 1px solid {BORDER};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = QLabel("ITEM DETAILS")
        self._header.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px; padding: 10px 12px 8px 12px;"
        )
        root.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {SURFACE0}; }}")
        root.addWidget(self._scroll, 1)

        body = QWidget()
        body.setStyleSheet(f"background: {SURFACE0};")
        self._body_lay = QVBoxLayout(body)
        self._body_lay.setContentsMargins(12, 8, 12, 16)
        self._body_lay.setSpacing(10)
        self._scroll.setWidget(body)

        self._section_lbl = QLabel("Nothing selected")
        self._section_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {TEAL}; letter-spacing: 1px;")
        self._body_lay.addWidget(self._section_lbl)
        _label_selectable(self._section_lbl)

        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(_ICON_ITEM_DETAIL, _ICON_ITEM_DETAIL)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body_lay.addWidget(self._icon_lbl, alignment=Qt.AlignmentFlag.AlignLeft)

        self._name_lbl = QLabel("Select an inventory item to inspect its details.")
        self._name_lbl.setWordWrap(True)
        self._name_lbl.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {TEXT};")
        self._body_lay.addWidget(self._name_lbl)
        _label_selectable(self._name_lbl)

        self._meta_lbl = QLabel("")
        self._meta_lbl.setWordWrap(True)
        self._meta_lbl.setStyleSheet(f"font-size: 12px; color: {SUBTEXT0};")
        self._body_lay.addWidget(self._meta_lbl)
        _label_selectable(self._meta_lbl)

        self._summary_lbl = QLabel("")
        self._summary_lbl.setWordWrap(True)
        self._summary_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT};")
        self._body_lay.addWidget(self._summary_lbl)
        _label_selectable(self._summary_lbl)

        self._stats_title = QLabel("STAT HIGHLIGHTS")
        self._stats_title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;"
        )
        self._body_lay.addWidget(self._stats_title)

        self._stats_host = QWidget()
        self._stats_host_lay = QGridLayout(self._stats_host)
        self._stats_host_lay.setContentsMargins(0, 0, 0, 0)
        self._stats_host_lay.setHorizontalSpacing(6)
        self._stats_host_lay.setVerticalSpacing(6)
        self._body_lay.addWidget(self._stats_host)

        self._properties_title = QLabel("PROPERTIES")
        self._properties_title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px;"
        )
        self._body_lay.addWidget(self._properties_title)

        self._properties_host = QWidget()
        self._properties_host_lay = QVBoxLayout(self._properties_host)
        self._properties_host_lay.setContentsMargins(0, 0, 0, 0)
        self._properties_host_lay.setSpacing(6)
        self._body_lay.addWidget(self._properties_host)

        self._description_title = QLabel("DESCRIPTION")
        self._description_title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px; padding-top: 4px;"
        )
        self._body_lay.addWidget(self._description_title)

        self._description_lbl = QLabel("")
        self._description_lbl.setWordWrap(True)
        self._description_lbl.setStyleSheet(
            f"font-size: 12px; color: {TEXT}; background: {SURFACE1}; border: 1px solid {BORDER}; border-radius: 6px;"
            " padding: 10px;"
        )
        self._body_lay.addWidget(self._description_lbl)
        _label_selectable(self._description_lbl)
        self._body_lay.addStretch()

        self.clear()

    def clear(self) -> None:
        self.set_item("", None)

    def set_item(self, source: str, item: dict[str, Any] | None) -> None:
        while self._stats_host_lay.count():
            child = self._stats_host_lay.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        while self._properties_host_lay.count():
            child = self._properties_host_lay.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if item is None:
            self._section_lbl.setText("Nothing selected")
            self._icon_lbl.setPixmap(_placeholder(_ICON_ITEM_DETAIL))
            self._name_lbl.setText("Select an inventory item to inspect its details.")
            self._meta_lbl.hide()
            self._summary_lbl.hide()
            self._stats_title.hide()
            self._stats_host.hide()
            self._properties_title.hide()
            self._properties_host.hide()
            self._description_title.hide()
            self._description_lbl.hide()
            return

        self._section_lbl.setText(source or "Item")
        self._icon_lbl.setPixmap(_item_icon_pixmap(item, _ICON_ITEM_DETAIL))
        self._name_lbl.setText(_display_item_name(item))

        meta_parts: list[str] = []
        if slot := str(item.get("Slot") or "").strip():
            meta_parts.append(slot)
        if kind := _item_kind_label(item):
            meta_parts.append(kind)
        if isinstance(item.get("Tier"), int):
            meta_parts.append(f"Tier {item['Tier']}")
        tags = item.get("Tags")
        if isinstance(tags, list) and tags:
            meta_parts.extend(f"[{tag}]" for tag in tags)
        self._meta_lbl.setText("  •  ".join(meta_parts))
        self._meta_lbl.setVisible(bool(meta_parts))

        preview = _item_preview_text(item)
        self._summary_lbl.setText(preview)
        self._summary_lbl.setVisible(bool(preview))

        stat_rows = _item_stat_highlights(item)
        self._stats_title.setVisible(bool(stat_rows))
        self._stats_host.setVisible(bool(stat_rows))
        for index, (label, value) in enumerate(stat_rows):
            chip = QLabel(f"{label}  {value}")
            chip.setStyleSheet(
                f"font-size: 11px; font-weight: 700; color: {TEXT}; background: {SURFACE1};"
                f" border: 1px solid {BORDER}; border-radius: 5px; padding: 7px 9px;"
            )
            self._stats_host_lay.addWidget(chip, index // 2, index % 2)

        property_rows = [(key, value) for key, value in _ordered_item_properties(item) if key != "Description"]
        self._properties_title.setVisible(bool(property_rows))
        self._properties_host.setVisible(bool(property_rows))
        for key, value in property_rows:
            row = QFrame()
            row.setStyleSheet(f"background: {SURFACE1}; border: 1px solid {BORDER}; border-radius: 6px;")
            row_lay = QVBoxLayout(row)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.setSpacing(2)

            key_lbl = QLabel(key.upper())
            key_lbl.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 0.7px;")
            row_lay.addWidget(key_lbl)
            _label_selectable(key_lbl)

            value_lbl = QLabel(value)
            value_lbl.setWordWrap(True)
            value_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT};")
            row_lay.addWidget(value_lbl)
            _label_selectable(value_lbl)
            self._properties_host_lay.addWidget(row)

        description = _item_description(item)
        self._description_title.setVisible(bool(description))
        self._description_lbl.setVisible(bool(description))
        self._description_lbl.setText(description)


# ── Main widget ───────────────────────────────────────────────────────────────


class CharacterSheetView(QWidget):
    """Visual character sheet: stats row + icon talent grid + detail panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detail_panel: _TalentDetailPanel | None = None
        self._talents_feature_lay: QVBoxLayout | None = None
        mark_startup_phase("sheet_detail_panel_deferred")
        self._current_category: str = ""
        self._current_sprite: QPixmap | None = None
        self._current_sprite_key: tuple[str, tuple[str, ...]] | None = None
        self._current_data: dict[str, Any] = {}
        self._current_char_name = ""
        self._game_connected = False
        self._live_equipment: list[dict[str, Any]] | None = None
        self._live_inventory: list[dict[str, Any]] | None = None
        self._live_transmog: list[dict[str, Any]] | None = None
        self._live_talents: dict[str, dict[str, Any]] | None = None
        self._live_sustains: dict[str, dict[str, Any]] | None = None
        self._live_effects: dict[str, dict[str, Any]] | None = None
        self._live_prodigies: list[dict[str, Any]] | None = None
        self._selected_inventory_source = ""
        self._selected_inventory_key: tuple[Any, ...] | None = None
        self._inventory_tab_index = -1
        self._inventory_dirty = True
        self._inventory_host_lay: QVBoxLayout | None = None
        self._equipped_panel: _ItemListPanel | None = None
        self._inventory_panel: _ItemListPanel | None = None
        self._transmog_panel: _ItemListPanel | None = None
        self._inventory_detail_panel: _InventoryDetailPanel | None = None
        self._progression_tab: Any | None = None
        self._progression_tab_index = -1
        self._progression_state: tuple[set[str], set[str], set[str], tuple[str, int, int] | None] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────
        mark_startup_phase("sheet_header_create_start")
        self._header = _HeaderBar()
        root.addWidget(self._header)
        mark_startup_phase("sheet_header_create_done")

        # ── Fixed player overview ────────────────────────────────────────
        mark_startup_phase("sheet_top_row_create_start")
        self._player_box = QFrame()
        self._player_box.setStyleSheet(f"background: {BG}; border-bottom: 1px solid {BORDER};")
        self._player_box_lay = QVBoxLayout(self._player_box)
        self._player_box_lay.setContentsMargins(12, 0, 12, 8)
        self._player_box_lay.setSpacing(0)
        root.addWidget(self._player_box)
        mark_startup_phase("sheet_top_row_create_done")

        # ── Content tabs ─────────────────────────────────────────────────
        mark_startup_phase("sheet_tabs_create_start")
        self._content_tabs = QTabWidget()
        root.addWidget(self._content_tabs, 1)
        mark_startup_phase("sheet_tabs_create_done")

        # Talents tab: left scroll | open feature area
        mark_startup_phase("sheet_talents_tab_create_start")
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
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {BG}; }}")
        self._talents_scroll = scroll
        self._left = QWidget()
        self._left.setStyleSheet(f"background: {BG};")
        self._left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._left_lay = QVBoxLayout(self._left)
        self._left_lay.setContentsMargins(12, 12, 12, 24)
        self._left_lay.setSpacing(2)
        self._left_lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._left_lay.addStretch()
        scroll.setWidget(self._left)
        splitter.addWidget(scroll)
        self._talents_feature_host = QWidget()
        self._talents_feature_host.setStyleSheet(f"background: {BG}; border-left: 1px solid {BORDER};")
        feature_lay = QVBoxLayout(self._talents_feature_host)
        feature_lay.setContentsMargins(12, 12, 12, 12)
        feature_lay.setSpacing(0)
        feature_lay.addStretch()
        self._talents_feature_lay = feature_lay
        splitter.addWidget(self._talents_feature_host)
        splitter.setSizes([860, 140])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        talents_root.addWidget(splitter)
        self._content_tabs.addTab(talents_tab, "Talents")
        mark_startup_phase("sheet_talents_tab_create_done")

        mark_startup_phase("sheet_inventory_placeholder_start")
        inventory_tab = QWidget()
        self._inventory_host_lay = QVBoxLayout(inventory_tab)
        self._inventory_host_lay.setContentsMargins(0, 0, 0, 0)
        self._inventory_host_lay.setSpacing(0)
        self._inventory_tab_index = self._content_tabs.addTab(inventory_tab, "Inventory")
        mark_startup_phase("sheet_inventory_placeholder_done")

        mark_startup_phase("sheet_progression_placeholder_start")
        self._progression_host = QWidget()
        self._progression_host_lay = QVBoxLayout(self._progression_host)
        self._progression_host_lay.setContentsMargins(0, 0, 0, 0)
        self._progression_host_lay.setSpacing(0)
        self._progression_tab_index = self._content_tabs.addTab(self._progression_host, "Progression")
        mark_startup_phase("sheet_progression_placeholder_done")

        mark_startup_phase("sheet_enemy_placeholder_start")
        self._enemy_host = QWidget()
        self._enemy_host_lay = QVBoxLayout(self._enemy_host)
        self._enemy_host_lay.setContentsMargins(0, 0, 0, 0)
        self._enemy_host_lay.setSpacing(0)
        self._content_tabs.addTab(self._enemy_host, "Enemies")
        self._content_tabs.currentChanged.connect(self._on_content_tab_changed)
        mark_startup_phase("sheet_enemy_placeholder_done")

        mark_startup_phase("sheet_overlay_create_start")
        self._connecting_overlay = QWidget(self)
        self._connecting_overlay.setStyleSheet(f"background: {BG};")
        overlay_lay = QVBoxLayout(self._connecting_overlay)
        overlay_lay.setContentsMargins(24, 24, 24, 24)
        overlay_lay.addStretch()
        overlay_title = QLabel("Connecting To Game")
        overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_title.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {TEXT};")
        overlay_subtitle = QLabel("Character data will load after a live game session is attached.")
        overlay_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_subtitle.setStyleSheet(f"font-size: 13px; color: {SUBTEXT0};")
        overlay_lay.addWidget(overlay_title)
        overlay_lay.addSpacing(8)
        overlay_lay.addWidget(overlay_subtitle)
        overlay_lay.addStretch()
        self._connecting_overlay.raise_()
        self._connecting_overlay.show()
        mark_startup_phase("sheet_overlay_create_done")

    # ── Public API ────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._connecting_overlay.setGeometry(self.rect())

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

    def load(self, data: dict, char_name: str = "", *, defer_reload: bool = False) -> None:
        if data == self._current_data and char_name == self._current_char_name:
            return
        self._current_data = data
        self._current_char_name = char_name
        self._inventory_dirty = True
        if defer_reload:
            self._header.update_from(data.get("Character", {}), char_name)
            return
        self._reload_current()

    def set_game_connected(self, connected: bool) -> None:
        if connected == self._game_connected:
            return
        self._game_connected = connected
        self._connecting_overlay.setVisible(not connected)

    def set_live_inventory(
        self,
        equipment: list[dict[str, Any]] | None,
        current: list[dict[str, Any]] | None,
        transmog: list[dict[str, Any]] | None,
    ) -> None:
        if equipment == self._live_equipment and current == self._live_inventory and transmog == self._live_transmog:
            return
        self._live_equipment = equipment
        self._live_inventory = current
        self._live_transmog = transmog
        self._inventory_dirty = True
        self._reload_current()

    def set_live_talents(self, sections: dict[str, dict[str, Any]] | None) -> None:
        if sections == self._live_talents:
            return
        self._live_talents = sections
        self._reload_current()

    def set_live_sustains(self, sections: dict[str, dict[str, Any]] | None) -> None:
        if sections == self._live_sustains:
            return
        self._live_sustains = sections
        self._reload_current()

    def set_live_effects(self, sections: dict[str, dict[str, Any]] | None) -> None:
        if sections == self._live_effects:
            return
        self._live_effects = sections
        self._reload_current()

    def set_live_prodigies(self, prodigies: list[dict[str, Any]] | None) -> None:
        if prodigies == self._live_prodigies:
            return
        self._live_prodigies = prodigies
        self._reload_current()

    def set_live_bundle(
        self,
        equipment: list[dict[str, Any]] | None,
        current: list[dict[str, Any]] | None,
        transmog: list[dict[str, Any]] | None,
        talents: dict[str, dict[str, Any]] | None,
        sustains: dict[str, dict[str, Any]] | None,
        effects: dict[str, dict[str, Any]] | None,
        prodigies: list[dict[str, Any]] | None,
    ) -> None:
        if (
            equipment == self._live_equipment
            and current == self._live_inventory
            and transmog == self._live_transmog
            and talents == self._live_talents
            and sustains == self._live_sustains
            and effects == self._live_effects
            and prodigies == self._live_prodigies
        ):
            return
        self._live_equipment = equipment
        self._live_inventory = current
        self._live_transmog = transmog
        self._live_talents = talents
        self._live_sustains = sustains
        self._live_effects = effects
        self._live_prodigies = prodigies
        self._inventory_dirty = True
        self._reload_current()

    def clear_live_inventory(self) -> None:
        if (
            self._live_equipment is None
            and self._live_inventory is None
            and self._live_transmog is None
            and self._live_talents is None
            and self._live_sustains is None
            and self._live_effects is None
            and self._live_prodigies is None
        ):
            return
        self._live_equipment = None
        self._live_inventory = None
        self._live_transmog = None
        self._live_talents = None
        self._live_sustains = None
        self._live_effects = None
        self._live_prodigies = None
        self._inventory_dirty = True
        self._reload_current()

    @staticmethod
    def _visible_talent_key(name: str) -> str:
        return name.replace("\u200b", "")

    def _normalized_category_name(self, name: str) -> str:
        visible = self._visible_talent_key(name)
        if "/" in visible:
            visible = visible.rsplit("/", 1)[-1]
        return " ".join(visible.split()).strip().lower()

    def _order_talent_section(
        self,
        section: dict[str, Any],
        file_section: dict[str, Any] | None,
    ) -> dict[str, Any]:
        blocks: list[tuple[str, list[tuple[str, Any]]]] = []
        current_header = ""
        current_block: list[tuple[str, Any]] = []

        for entry_name, entry_data in section.items():
            if _is_category_header(entry_data):
                if current_block:
                    blocks.append((current_header, current_block))
                current_header = self._visible_talent_key(entry_name)
                current_block = [(entry_name, entry_data)]
            else:
                current_block.append((entry_name, entry_data))

        if current_block:
            blocks.append((current_header, current_block))

        if not isinstance(file_section, dict) or not file_section:
            ordered: dict[str, Any] = {}
            for _header, block in blocks:
                for entry_name, entry_data in block:
                    ordered[entry_name] = entry_data
            return ordered

        file_headers = [
            self._normalized_category_name(name) for name, value in file_section.items() if _is_category_header(value)
        ]
        block_lookup = {self._normalized_category_name(header): block for header, block in blocks}

        ordered: dict[str, Any] = {}
        used_headers: set[str] = set()
        for header in file_headers:
            block = block_lookup.get(header)
            if block is None:
                continue
            used_headers.add(header)
            for entry_name, entry_data in block:
                ordered[entry_name] = entry_data
        for header, block in blocks:
            if header in used_headers:
                continue
            for entry_name, entry_data in block:
                ordered[entry_name] = entry_data
        return ordered

    def _merge_live_talents(self) -> dict[str, dict[str, Any]]:
        if not self._live_talents:
            return {}

        file_talents: dict[str, dict[str, Any]] = {}
        for key, value in self._current_data.items():
            if "Talents" not in key or not isinstance(value, dict):
                continue
            file_talents[key] = value

        by_name: dict[str, Any] = {}
        for section in file_talents.values():
            for talent_name, talent_data in section.items():
                if _is_category_header(talent_data):
                    continue
                by_name[talent_name] = talent_data

        merged: dict[str, dict[str, Any]] = {}
        for section_name, section in self._live_talents.items():
            merged_section: dict[str, Any] = {}
            for entry_name, entry_data in section.items():
                final_data = entry_data
                if _is_category_header(entry_data):
                    merged_section[entry_name] = entry_data
                    continue
                if isinstance(entry_data, dict):
                    combined = dict(entry_data)
                    file_data = by_name.get(entry_name)
                    if isinstance(file_data, dict):
                        for label in (
                            "Travel Speed",
                            "Usage Speed",
                            "Scales With",
                            "Turn Duration",
                            "Stats",
                            "Stats per turn",
                            "Description",
                        ):
                            if label not in combined and label in file_data:
                                combined[label] = file_data[label]
                        final_data = combined
                merged_section[entry_name] = final_data
            merged[section_name] = merged_section
        return merged

    def _reload_current(self) -> None:
        talent_scroll_value = self._talents_scroll.verticalScrollBar().value()
        self._clear_left()
        self._clear_player_box()
        char = self._current_data.get("Character", {})
        self._header.update_from(char, self._current_char_name)

        # Stats row + live sustain/effect icons
        stats = self._current_data.get("Primary Stats", {})
        if not isinstance(stats, dict):
            stats = {}
        if stats or self._current_sprite is not None or self._live_sustains or self._live_effects:
            overview = _PlayerOverview(
                stats,
                sprite=self._current_sprite,
                sustains=self._live_sustains,
                effects=self._live_effects,
            )
            overview.clicked.connect(self._on_summary_talent_clicked)
            self._player_box_lay.addWidget(overview)

        # Talent sections
        talent_sections = self._merge_live_talents()
        if not talent_sections:
            talent_sections = {
                key: value
                for key, value in self._current_data.items()
                if "Talents" in key and isinstance(value, dict) and value
            }
        if talent_sections:
            self._insert(self._left_lay, self._build_talent_columns(talent_sections))

        if self._content_tabs.currentIndex() == self._inventory_tab_index:
            self._reload_inventory_panels()
        self._left.adjustSize()
        QTimer.singleShot(
            0,
            lambda value=talent_scroll_value: self._talents_scroll.verticalScrollBar().setValue(value),
        )

    def _restore_inventory_selection(self, sections: tuple[tuple[str, list[dict[str, Any]]], ...]) -> None:
        if (
            self._equipped_panel is None
            or self._inventory_panel is None
            or self._transmog_panel is None
            or self._inventory_detail_panel is None
        ):
            return
        if self._selected_inventory_key is not None and self._selected_inventory_source:
            for source, items in sections:
                if source != self._selected_inventory_source:
                    continue
                for item in items:
                    if _inventory_item_key(item) == self._selected_inventory_key:
                        self._set_selected_inventory(source, item)
                        return

        for source, items in sections:
            if items:
                self._set_selected_inventory(source, items[0])
                return

        self._selected_inventory_source = ""
        self._selected_inventory_key = None
        self._equipped_panel.set_selected_key(None)
        self._inventory_panel.set_selected_key(None)
        self._transmog_panel.set_selected_key(None)
        self._inventory_detail_panel.clear()

    def _set_selected_inventory(self, source: str, item: dict[str, Any]) -> None:
        if (
            self._equipped_panel is None
            or self._inventory_panel is None
            or self._transmog_panel is None
            or self._inventory_detail_panel is None
        ):
            return
        self._selected_inventory_source = source
        self._selected_inventory_key = _inventory_item_key(item)
        self._equipped_panel.set_selected_key(
            self._selected_inventory_key if source == self._equipped_panel.title else None
        )
        self._inventory_panel.set_selected_key(
            self._selected_inventory_key if source == self._inventory_panel.title else None
        )
        self._transmog_panel.set_selected_key(
            self._selected_inventory_key if source == self._transmog_panel.title else None
        )
        self._inventory_detail_panel.set_item(source, item)

    def _on_inventory_item_selected(self, source: str, item: object) -> None:
        if isinstance(item, dict):
            self._set_selected_inventory(source, item)

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
        uniques: set[str],
        current_zone: tuple[str, int, int] | None,
    ) -> None:
        self._progression_state = (visited, deaths, uniques, current_zone)
        if self._progression_tab is not None:
            self._progression_tab.update(visited, deaths, uniques, current_zone)

    def _on_content_tab_changed(self, index: int) -> None:
        if index == self._inventory_tab_index:
            self._reload_inventory_panels()
        if index == self._progression_tab_index:
            self._ensure_progression_tab()

    def _reload_inventory_panels(self) -> None:
        if not self._inventory_dirty:
            return
        if not self._ensure_inventory_tab():
            return

        file_equipment = self._current_data.get("Equipment", [])
        inventory = self._current_data.get("Inventory", [])
        file_equipment_items = file_equipment if isinstance(file_equipment, list) else []
        if isinstance(inventory, dict):
            file_current = inventory.get("Current", [])
            transmog_items = inventory.get("Transmog Chest", [])
        elif isinstance(inventory, list):
            file_current = inventory
            transmog_items = []
        else:
            file_current = []
            transmog_items = []
        equipment_items = _merge_item_sources(self._live_equipment, file_equipment_items)
        current_items = _merge_item_sources(
            self._live_inventory,
            file_current if isinstance(file_current, list) else [],
        )
        transmog_live = _merge_item_sources(
            self._live_transmog,
            transmog_items if isinstance(transmog_items, list) else [],
        )
        equipped_records = self._equipped_panel.set_items(equipment_items, show_slot=True)
        current_records = self._inventory_panel.set_items(
            current_items,
            show_slot=False,
        )
        transmog_records = self._transmog_panel.set_items(
            transmog_live,
            show_slot=False,
        )
        self._restore_inventory_selection(
            (
                (self._equipped_panel.title, equipped_records),
                (self._inventory_panel.title, current_records),
                (self._transmog_panel.title, transmog_records),
            )
        )
        self._inventory_dirty = False

    def _ensure_inventory_tab(self) -> bool:
        if (
            self._equipped_panel is not None
            and self._inventory_panel is not None
            and self._transmog_panel is not None
            and self._inventory_detail_panel is not None
        ):
            return True
        if self._inventory_host_lay is None:
            return False

        inventory_workspace = QSplitter(Qt.Orientation.Horizontal)
        inventory_workspace.setHandleWidth(1)
        inventory_workspace.setChildrenCollapsible(False)
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
        self._inventory_detail_panel = _InventoryDetailPanel()
        inventory_workspace.addWidget(inventory_splitter)
        inventory_workspace.addWidget(self._inventory_detail_panel)
        inventory_workspace.setSizes([900, 360])
        inventory_workspace.setStretchFactor(0, 2)
        inventory_workspace.setStretchFactor(1, 1)
        self._inventory_host_lay.addWidget(inventory_workspace)
        self._equipped_panel.item_selected.connect(self._on_inventory_item_selected)
        self._inventory_panel.item_selected.connect(self._on_inventory_item_selected)
        self._transmog_panel.item_selected.connect(self._on_inventory_item_selected)
        return True

    def _ensure_progression_tab(self) -> Any:
        if self._progression_tab is not None:
            return self._progression_tab

        from gui.progression_tab import ProgressionTab

        panel = ProgressionTab()
        self._progression_tab = panel
        self._progression_host_lay.addWidget(panel)
        if self._progression_state is not None:
            panel.update(*self._progression_state)
        return panel

    # ── Builders ─────────────────────────────────────────────────────────

    def build_talent_section(self, title: str, talents: dict) -> QWidget:
        return self._build_talent_section(title, talents)

    def _build_talent_columns(self, sections: dict[str, dict[str, Any]]) -> QWidget:
        wrapper = QWidget()
        wrapper.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(wrapper)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        class_section = sections.get("Class Talents")
        generic_section = sections.get("Generic Talents")
        extra_sections = [
            (section_title, section_data)
            for section_title, section_data in sections.items()
            if section_title not in {"Class Talents", "Generic Talents"}
        ]

        def add_section(section_title: str, section_data: dict[str, Any] | None) -> None:
            if not isinstance(section_data, dict) or not section_data:
                return
            section_widget = self._build_talent_section(section_title, section_data)
            section_widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            lay.addWidget(section_widget, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        def add_divider() -> None:
            divider = QFrame()
            divider.setFrameShape(QFrame.Shape.VLine)
            divider.setLineWidth(1)
            divider.setStyleSheet(f"color: {BORDER}; background: {BORDER};")
            lay.addWidget(divider)

        add_section("Class Talents", class_section)
        if isinstance(class_section, dict) and class_section and isinstance(generic_section, dict) and generic_section:
            add_divider()
        add_section("Generic Talents", generic_section)

        if lay.count() == 0:
            for section_title, section_data in sections.items():
                add_section(section_title, section_data)
        else:
            for section_title, section_data in extra_sections:
                if not isinstance(section_data, dict) or not section_data:
                    continue
                add_divider()
                add_section(section_title, section_data)

        # Prodigies column — sourced from live memory, level 25+ only
        if self._live_prodigies:
            add_divider()
            prodigy_col = self._build_prodigy_column(self._live_prodigies)
            prodigy_col.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            lay.addWidget(prodigy_col, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        lay.addStretch(1)
        return wrapper

    def _build_prodigy_column(self, prodigies: list[dict[str, Any]]) -> QWidget:
        """Build a compact prodigy column showing available-to-learn prodigies."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 4)
        lay.setSpacing(4)

        title = QLabel("PRODIGIES")
        title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px; padding-bottom: 2px;"
        )
        lay.addWidget(title)

        gw = QWidget()
        gl = QGridLayout(gw)
        gl.setContentsMargins(0, 2, 0, 6)
        gl.setSpacing(4)
        for idx, prodigy in enumerate(prodigies):
            name = str(prodigy.get("Name") or "")
            if not name:
                continue
            detail = dict(prodigy)
            detail.pop("Name", None)
            icon_w = _TalentIcon(name, detail)
            icon_w.clicked.connect(lambda n, d: self._on_talent_clicked(n, d, "Prodigies"))
            gl.addWidget(icon_w, idx // 2, idx % 2)
        lay.addWidget(gw)
        return w

    def _build_talent_section(self, title: str, talents: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 4)
        lay.setSpacing(4)

        # Section title (e.g. "CLASS TALENTS")
        sec_lbl = QLabel(title.upper())
        sec_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {SUBTEXT0}; letter-spacing: 1px; padding-bottom: 2px;"
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
                icon_w.clicked.connect(lambda n, d, cat=current_category: self._on_talent_clicked(n, d, cat))
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
        self._show_talent_detail(name, data, category)

    def _on_summary_talent_clicked(self, name: str, data: Any, category: str) -> None:
        self._content_tabs.setCurrentIndex(0)
        self._show_talent_detail(name, data, category)

    def _show_talent_detail(self, name: str, data: Any, category: str) -> None:
        self._current_category = category
        self._ensure_detail_panel().show_talent(name, data, category)

    def _ensure_detail_panel(self) -> _TalentDetailPanel:
        if self._detail_panel is not None:
            return self._detail_panel
        panel = _TalentDetailPanel()
        self._detail_panel = panel
        if self._talents_feature_lay is not None:
            self._talents_feature_lay.insertWidget(0, panel, 0, Qt.AlignmentFlag.AlignTop)
        return panel

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
                    return _trim_transparent_bounds(
                        raw.scaled(
                            _ICON_SPRITE,
                            _ICON_SPRITE,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation,
                        )
                    )
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
        cls = char.get("Class", "")
        race = char.get("Race", "")
        level = char.get("Level / Exp", "").split(" ")[0]
        mode = char.get("Mode", "")

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

    _HP_STYLE = "font-size: 13px; font-weight: 700; padding-left: 8px; padding-right: 4px;"
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
