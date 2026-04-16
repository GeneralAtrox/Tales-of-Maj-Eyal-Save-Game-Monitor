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
_compose_cache: dict[tuple[str, ...], QPixmap] = {}  # layer tuple → QPixmap


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


def compose_layers(image_hints: list[str], size: int = 64) -> QPixmap | None:
    """
    Stack *image_hints* as PNG layers (first = bottom, last = top) and return
    a composited *size* × *size* QPixmap.

    Layers are extracted from the gfx pack on first use and cached locally.
    The composed result is cached in memory so repeated calls are free.
    Returns None if no layer could be loaded.
    """
    key = tuple(image_hints)
    if key in _compose_cache:
        return _compose_cache[key]

    paths = [get_sprite(hint) for hint in image_hints]
    paths = [p for p in paths if p is not None]
    if not paths:
        return None

    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    for p in paths:
        layer = QPixmap(str(p))
        if layer.isNull():
            continue
        scaled = layer.scaled(
            QSize(size, size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Centre the layer on the canvas
        x = (size - scaled.width())  // 2
        y = (size - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

    painter.end()

    _compose_cache[key] = canvas
    return canvas
