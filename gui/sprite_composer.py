"""
sprite_composer.py
------------------
On-demand sprite extraction and layer compositing for ToME entities.

Composite sprites (e.g. the runic golem) are stored as an ordered list of
PNG layers in add_mos.  This module extracts each layer from tome-gfx.team
on first use (caching it under Icons/) then stacks them with QPainter to
produce a single QPixmap.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap

# ── Paths ─────────────────────────────────────────────────────────────────────

_GFX_TEAM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome-gfx.team"
)
_ICONS_ROOT   = Path(__file__).resolve().parent.parent / "Icons"
_GFX_PREFIX   = "data/gfx/shockbolt/"   # path inside the zip

# ── In-memory caches ──────────────────────────────────────────────────────────

_extract_cache: dict[str, Path | None] = {}         # image_hint → local Path
_compose_cache: dict[tuple[tuple[str, ...], int, bool], QPixmap] = {}  # (layers, size, align_bottom) → QPixmap

_PLAYER_BODY_LAYER_PREFIXES = (
    "base_",
    "lower_body_",
    "upper_body_",
    "cloak_shoulder_",
    "head_",
    "hair_",
    "right_hand_",
)


# ── Public API ────────────────────────────────────────────────────────────────

def get_sprite(image_hint: str) -> Path | None:
    """
    Return a local Path for *image_hint* (e.g. ``"player/runic_golem/base_02.png"``),
    extracting it from tome-gfx.team if it is not already on disk.
    Returns None if the file cannot be found in the archive.
    """
    if image_hint in _extract_cache:
        return _extract_cache[image_hint]

    local = _ICONS_ROOT / image_hint
    if local.exists():
        _extract_cache[image_hint] = local
        return local

    if not _GFX_TEAM.exists():
        _extract_cache[image_hint] = None
        return None

    arc_path = _GFX_PREFIX + image_hint
    try:
        with zipfile.ZipFile(_GFX_TEAM) as zf:
            data = zf.read(arc_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        _extract_cache[image_hint] = local
        return local
    except (KeyError, zipfile.BadZipFile):
        pass
    except Exception:
        pass

    _extract_cache[image_hint] = None
    return None


def normalize_sprite_layers(image_hint: str, sprite_layers: list[str]) -> tuple[list[str], bool]:
    """
    Apply sprite-selection policy before composition.

    Returns ``(layers, align_bottom)`` where ``layers`` are deduplicated and
    filtered for the actor type, and ``align_bottom`` indicates whether shorter
    layers should anchor to the bottom of the shared native canvas.
    """
    distinct_layers = list(dict.fromkeys(sprite_layers))
    is_player = image_hint.startswith("player/") or any(layer.startswith("player/") for layer in distinct_layers)
    if not is_player:
        return distinct_layers, False

    filtered_layers: list[str] = []
    for layer in distinct_layers:
        if not layer.startswith("player/"):
            continue
        stem = Path(layer).name
        if stem.startswith(_PLAYER_BODY_LAYER_PREFIXES):
            filtered_layers.append(layer)
    return filtered_layers or distinct_layers, True


def compose_layers(image_hints: list[str], size: int = 64, align_bottom: bool = False) -> QPixmap | None:
    """
    Stack *image_hints* as PNG layers (first = bottom, last = top) and return
    a composited *size* × *size* QPixmap.

    Layers are extracted from the gfx pack on first use and cached locally.
    The composed result is cached in memory so repeated calls are free.
    Returns None if no layer could be loaded.
    """
    key = (tuple(image_hints), size, align_bottom)
    if key in _compose_cache:
        return _compose_cache[key]

    layers = [QPixmap(str(path)) for path in (get_sprite(hint) for hint in image_hints) if path is not None]
    layers = [layer for layer in layers if not layer.isNull()]
    if not layers:
        return None

    native_width = max(layer.width() for layer in layers)
    native_height = max(layer.height() for layer in layers)

    native = QPixmap(native_width, native_height)
    native.fill(Qt.GlobalColor.transparent)
    painter = QPainter(native)

    for layer in layers:
        # Preserve each layer's original coordinate space; scale only once after compositing.
        y = native_height - layer.height() if align_bottom and layer.height() < native_height else 0
        painter.drawPixmap(0, y, layer)

    painter.end()

    scaled = native.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
    painter.end()

    _compose_cache[key] = canvas
    return canvas
