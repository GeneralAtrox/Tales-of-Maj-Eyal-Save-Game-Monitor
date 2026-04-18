"""
game_data/talent_db.py
----------------------
Parses talent definitions from the ToME game archive (tome.team) and exposes
name-keyed metadata for GUI talent panels.

The database is built lazily on first use and cached as JSON beside this
module, so repeated launches avoid re-scanning the archive unless it changes.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_TOME_TEAM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome.team"
)
_CACHE_FILE = Path(__file__).parent / "_talent_cache.json"
_CACHE_SCHEMA_VERSION = 2


@dataclass(slots=True)
class TalentRecord:
    description: str = ""
    icon: str = ""


_db: dict[str, TalentRecord] | None = None

_RE_NAME = re.compile(r'\bname\s*=\s*(?:_t)?\"([^\"]+)\"')
_RE_DESC_BLOCK = re.compile(r"\bdesc\s*=\s*(?:_t)?\[\[(.*?)\]\]", re.DOTALL)
_RE_DESC_LINE = re.compile(r'\bdesc\s*=\s*(?:_t)?\"((?:[^"\\]|\\.)*)\"')
_RE_IMAGE = re.compile(r'\bimage\s*=\s*"([^"]+)"')


def get_talent_db() -> dict[str, TalentRecord]:
    """Return a name-keyed map of talent metadata."""
    global _db
    if _db is None:
        _db = _load_or_build()
    return _db


def lookup_talent_description(name: str) -> str:
    """Return a talent description by display name, or an empty string."""
    return get_talent_db().get(name, TalentRecord()).description


def lookup_talent_icon(name: str) -> str:
    """Return a normalized icon filename by display name, or an empty string."""
    return get_talent_db().get(name, TalentRecord()).icon


def _load_or_build() -> dict[str, TalentRecord]:
    if not _TOME_TEAM.exists():
        return {}

    if _CACHE_FILE.exists() and _CACHE_FILE.stat().st_mtime > _TOME_TEAM.stat().st_mtime:
        cached = _load_cache()
        if cached:
            return cached

    db = _build_db()
    _save_cache(db)
    return db


def _build_db() -> dict[str, TalentRecord]:
    db: dict[str, TalentRecord] = {}
    try:
        with zipfile.ZipFile(_TOME_TEAM) as zf:
            talent_paths = [
                name
                for name in zf.namelist()
                if name.startswith("data/talents/") and name.endswith(".lua")
            ]
            for path in talent_paths:
                lua = zf.read(path).decode("utf-8", errors="replace")
                for name, record in _parse_lua(lua):
                    if name not in db:
                        db[name] = record
    except Exception:  # noqa: BLE001
        return {}
    return db


def _parse_lua(lua: str) -> list[tuple[str, TalentRecord]]:
    results: list[tuple[str, TalentRecord]] = []
    for block in _split_talents(lua):
        name_match = _RE_NAME.search(block)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        results.append(
            (
                name,
                TalentRecord(
                    description=_extract_description(block),
                    icon=_extract_icon(block),
                ),
            )
        )
    return results


def _split_talents(lua: str) -> list[str]:
    """Return each raw ``newTalent{ ... }`` block."""
    pattern = re.compile(r"newTalent\s*\{(.*?)^\}", re.DOTALL | re.MULTILINE)
    return [f"newTalent{{{body}\n}}" for body in pattern.findall(lua)]


def _extract_description(block: str) -> str:
    if desc_match := _RE_DESC_BLOCK.search(block):
        return _normalize_description(desc_match.group(1))
    if desc_match := _RE_DESC_LINE.search(block):
        return _normalize_description(bytes(desc_match.group(1), "utf-8").decode("unicode_escape"))
    return ""


def _extract_icon(block: str) -> str:
    if not (match := _RE_IMAGE.search(block)):
        return ""
    image = match.group(1).strip()
    if not image.endswith(".png"):
        return ""
    return PurePosixPath(image).name


def _normalize_description(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact = " ".join(line for line in lines if line)
    return re.sub(r"\s+", " ", compact).strip()


def _load_cache() -> dict[str, TalentRecord]:
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return {}
    records = raw.get("records")
    if not isinstance(records, dict):
        return {}

    result: dict[str, TalentRecord] = {}
    for name, value in records.items():
        if not isinstance(name, str):
            continue
        if not isinstance(value, dict):
            continue
        result[name] = TalentRecord(
            description=str(value.get("description", "")),
            icon=str(value.get("icon", "")),
        )
    return result


def _save_cache(db: dict[str, TalentRecord]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(
                {
                    "schema_version": _CACHE_SCHEMA_VERSION,
                    "records": {
                        name: {"description": record.description, "icon": record.icon}
                        for name, record in db.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
