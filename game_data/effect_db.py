"""
game_data/effect_db.py
----------------------
Parses ToME timed-effect definitions from ``tome.team`` and exposes a
cached ``EFF_*`` → metadata mapping for the live dashboard.

We only need static metadata here:

- display name (``desc``)
- icon path
- broad type/status (physical / magical / mental / other,
  beneficial / detrimental)
- whether the duration counts down
- a best-effort long-description summary for the detail panel

Effect *state* (remaining duration, source actor, power, stacks, ...)
still comes from live memory via ``player.tmp``.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

from game_data.lua_extractor import (
    RE_DESC_BLOCK,
    RE_DESC_LINE,
    RE_IMAGE,
    find_tome_team,
    iter_balanced_blocks,
)

_EFFECT_FILES = (
    "data/timed_effects/magical.lua",
    "data/timed_effects/physical.lua",
    "data/timed_effects/mental.lua",
    "data/timed_effects/other.lua",
    "data/timed_effects/floor.lua",
)

_RE_NAME = re.compile(r'\bname\s*=\s*"([^"]+)"')
_RE_TYPE = re.compile(r'\btype\s*=\s*"([^"]+)"')
_RE_STATUS = re.compile(r'\bstatus\s*=\s*"([^"]+)"')
_RE_DECREASE = re.compile(r"\bdecrease\s*=\s*(-?\d+)")
_RE_LONG_DESC_BLOCK = re.compile(
    r"\blong_desc\s*=\s*function\b.*?\breturn\s*(?:\(?\s*)?(?:_t)?\[\[(.*?)\]\]",
    re.DOTALL,
)
_RE_LONG_DESC_LINE = re.compile(
    r'\blong_desc\s*=\s*function\b.*?\breturn\s*(?:\(?\s*)?(?:_t)?\"((?:[^"\\]|\\.)*)\"',
    re.DOTALL,
)


@dataclass(slots=True)
class EffectRecord:
    effect_id: str
    name: str
    icon: str
    description: str
    summary: str
    effect_type: str
    status: str
    decrease: int


_db_by_id: dict[str, EffectRecord] | None = None


def get_effect_db_by_id() -> dict[str, EffectRecord]:
    """Return a cached map of ``EFF_*`` ids to effect metadata."""
    global _db_by_id
    if _db_by_id is None:
        _db_by_id = _build_db()
    return _db_by_id


def lookup_effect_by_id(effect_id: str) -> EffectRecord | None:
    return get_effect_db_by_id().get(effect_id)


def _build_db() -> dict[str, EffectRecord]:
    team_path = find_tome_team()
    if team_path is None:
        return {}

    db: dict[str, EffectRecord] = {}
    try:
        with zipfile.ZipFile(team_path, "r") as zf:
            names_in_zip = set(zf.namelist())
            for lua_path in _EFFECT_FILES:
                if lua_path not in names_in_zip:
                    continue
                try:
                    src = zf.read(lua_path).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                for block in iter_balanced_blocks(src, "newEffect"):
                    record = _parse_effect_block(block)
                    if record is None:
                        continue
                    db[record.effect_id] = record
    except Exception:  # noqa: BLE001
        return {}
    return db


def _parse_effect_block(block: str) -> EffectRecord | None:
    name_match = _RE_NAME.search(block)
    desc = _extract_desc(block)
    effect_type = _extract_first(block, _RE_TYPE).lower()
    if not name_match or not desc or not effect_type:
        return None

    raw_name = name_match.group(1).strip().upper()
    icon = _extract_first(block, RE_IMAGE)
    if not icon:
        icon = f"effects/{raw_name.lower()}.png"

    summary = _extract_long_desc(block)
    status = _extract_first(block, _RE_STATUS).lower() or "detrimental"
    decrease = _extract_int(block, _RE_DECREASE, default=1)

    return EffectRecord(
        effect_id=f"EFF_{raw_name}",
        name=desc,
        icon=icon,
        description=desc,
        summary=summary,
        effect_type=effect_type,
        status=status,
        decrease=decrease,
    )


def _extract_desc(block: str) -> str:
    if match := RE_DESC_BLOCK.search(block):
        return _normalize_text(match.group(1))
    if match := RE_DESC_LINE.search(block):
        raw = bytes(match.group(1), "utf-8").decode("unicode_escape")
        return _normalize_text(raw)
    return ""


def _extract_long_desc(block: str) -> str:
    if match := _RE_LONG_DESC_BLOCK.search(block):
        return _normalize_text(match.group(1))
    if match := _RE_LONG_DESC_LINE.search(block):
        raw = bytes(match.group(1), "utf-8").decode("unicode_escape")
        return _normalize_text(raw)
    return ""


def _extract_first(block: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(block)
    return match.group(1).strip() if match else ""


def _extract_int(block: str, pattern: re.Pattern[str], *, default: int) -> int:
    match = pattern.search(block)
    if not match:
        return default
    try:
        return int(match.group(1))
    except ValueError:
        return default


def _normalize_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact = " ".join(line for line in lines if line)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact
