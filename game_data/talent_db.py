"""
game_data/talent_db.py
----------------------
Parses talent definitions from the ToME game archive (tome.team) and exposes
name- and id-keyed metadata for GUI talent panels and threat scoring.

The database is built lazily on first use and cached as JSON beside this
module, so repeated launches avoid re-scanning the archive unless it changes.

Schema v4: stores both name- and id-keyed records so duplicate display names
do not drop engine ids used by NPC and boss talent tables.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_TOME_TEAM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome.team"
)
_CACHE_FILE = Path(__file__).parent / "_talent_cache.json"
_CACHE_SCHEMA_VERSION = 4


@dataclass(slots=True)
class TalentRecord:
    description: str = ""
    icon: str = ""
    talent_id: str = ""
    """In-engine key like ``T_FLAME``. Empty when not derivable."""
    damage_type: str = ""
    """Uppercase ToME damage type (``FIRE``, ``PHYSICAL``, ...). Empty if the
    talent does no direct damage or we couldn't parse it."""
    scaling_family: str = ""
    """One of ``spell``, ``mind``, ``physical``, ``weapon``, ``flat``, or empty."""
    damage_low: float = 0.0
    damage_high: float = 0.0
    cooldown: int = 0
    tactical_disable: list[str] = field(default_factory=list)
    """Raw tactical-disable tags (``stun``, ``pin``, ``disarm``, ...)."""
    talent_type: str = ""
    """Full category, e.g. ``spell/fire``."""
    mode: str = ""
    """``activated`` | ``sustained`` | ``passive`` (empty if unparsed)."""


_db: dict[str, TalentRecord] | None = None
_db_by_id: dict[str, TalentRecord] | None = None

_RE_NAME = re.compile(r'\bname\s*=\s*(?:_t)?\"([^\"]+)\"')
_RE_SHORT_NAME = re.compile(r'\bshort_name\s*=\s*\"([^\"]+)\"')
_RE_DESC_BLOCK = re.compile(r"\bdesc\s*=\s*(?:_t)?\[\[(.*?)\]\]", re.DOTALL)
_RE_DESC_LINE = re.compile(r'\bdesc\s*=\s*(?:_t)?\"((?:[^"\\]|\\.)*)\"')
_RE_IMAGE = re.compile(r'\bimage\s*=\s*"([^"]+)"')
_RE_TYPE = re.compile(r'\btype\s*=\s*\{\s*"([^"]+)"')
_RE_MODE = re.compile(r'\bmode\s*=\s*"([^"]+)"')
_RE_COOLDOWN = re.compile(r"\bcooldown\s*=\s*(\d+)\b")
_RE_DAM_DESC = re.compile(r"damDesc\s*\(\s*[^,]+,\s*DamageType[.:](\w+)")
_RE_SCALING = re.compile(
    r"self:combatTalent(Spell|Mind|Physical|Weapon)Damage"
    r"\s*\(\s*t\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
)
_RE_TACTICAL_DISABLE = re.compile(r"\btactical\s*=\s*\{[^}]*?\bdisable\s*=\s*\{([^}]*)\}", re.DOTALL)
_RE_TACTICAL_KEY = re.compile(r"(\w+)\s*=")


def get_talent_db() -> dict[str, TalentRecord]:
    """Return a name-keyed map of talent metadata."""
    global _db
    if _db is None:
        _rebuild()
    assert _db is not None
    return _db


def get_talent_db_by_id() -> dict[str, TalentRecord]:
    """Return an id-keyed (``T_XXX``) map of talent metadata."""
    global _db_by_id
    if _db_by_id is None:
        _rebuild()
    assert _db_by_id is not None
    return _db_by_id


def lookup_talent_description(name: str) -> str:
    return get_talent_db().get(name, TalentRecord()).description


def lookup_talent_icon(name: str) -> str:
    return get_talent_db().get(name, TalentRecord()).icon


def lookup_talent_by_id(talent_id: str) -> TalentRecord | None:
    return get_talent_db_by_id().get(talent_id)


def _rebuild() -> None:
    global _db, _db_by_id
    _db, _db_by_id = _load_or_build()


def _load_or_build() -> tuple[dict[str, TalentRecord], dict[str, TalentRecord]]:
    if not _TOME_TEAM.exists():
        return {}, {}

    if _CACHE_FILE.exists() and _CACHE_FILE.stat().st_mtime > _TOME_TEAM.stat().st_mtime:
        cached = _load_cache()
        if cached:
            return cached

    db, db_by_id = _build_db()
    _save_cache(db, db_by_id)
    return db, db_by_id


def _build_db() -> tuple[dict[str, TalentRecord], dict[str, TalentRecord]]:
    db: dict[str, TalentRecord] = {}
    db_by_id: dict[str, TalentRecord] = {}
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
                    if record.talent_id and record.talent_id not in db_by_id:
                        db_by_id[record.talent_id] = record
    except Exception:  # noqa: BLE001
        return {}, {}
    return db, db_by_id


def _parse_lua(lua: str) -> list[tuple[str, TalentRecord]]:
    results: list[tuple[str, TalentRecord]] = []
    for block in _split_talents(lua):
        name_match = _RE_NAME.search(block)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        record = TalentRecord(
            description=_extract_description(block),
            icon=_extract_icon(block),
            talent_id=_extract_talent_id(block, name),
            talent_type=_extract_first(block, _RE_TYPE),
            mode=_extract_first(block, _RE_MODE),
            cooldown=_extract_cooldown(block),
            tactical_disable=_extract_tactical_disable(block),
        )
        dtype, family, low, high = _extract_damage(block)
        record.damage_type = dtype
        record.scaling_family = family
        record.damage_low = low
        record.damage_high = high
        results.append((name, record))
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


def _extract_first(block: str, pattern: re.Pattern[str]) -> str:
    m = pattern.search(block)
    return m.group(1).strip() if m else ""


def _extract_cooldown(block: str) -> int:
    m = _RE_COOLDOWN.search(block)
    return int(m.group(1)) if m else 0


def _extract_tactical_disable(block: str) -> list[str]:
    m = _RE_TACTICAL_DISABLE.search(block)
    if not m:
        return []
    body = m.group(1)
    return sorted({k.lower() for k in _RE_TACTICAL_KEY.findall(body)})


def _extract_talent_id(block: str, name: str) -> str:
    if m := _RE_SHORT_NAME.search(block):
        sn = m.group(1).strip().upper()
        return sn if sn.startswith("T_") else f"T_{sn}"
    # Derive from name: uppercase, non-alnum → underscore, collapse, strip.
    derived = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"T_{derived}" if derived else ""


def _extract_damage(block: str) -> tuple[str, str, float, float]:
    dtype = ""
    if m := _RE_DAM_DESC.search(block):
        dtype = m.group(1).upper()
    family = ""
    low = 0.0
    high = 0.0
    if m := _RE_SCALING.search(block):
        family = m.group(1).lower()  # spell|mind|physical|weapon
        try:
            low = float(m.group(2))
            high = float(m.group(3))
        except ValueError:
            low = high = 0.0
    return dtype, family, low, high


def _normalize_description(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact = " ".join(line for line in lines if line)
    return re.sub(r"\s+", " ", compact).strip()


def _load_cache() -> tuple[dict[str, TalentRecord], dict[str, TalentRecord]] | None:
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return None
    records = raw.get("records")
    records_by_id = raw.get("records_by_id")
    if not isinstance(records, dict) or not isinstance(records_by_id, dict):
        return None

    name_result: dict[str, TalentRecord] = {}
    for name, value in records.items():
        if not isinstance(name, str):
            continue
        record = _record_from_cache(value)
        if record is not None:
            name_result[name] = record

    id_result: dict[str, TalentRecord] = {}
    for talent_id, value in records_by_id.items():
        if not isinstance(talent_id, str):
            continue
        record = _record_from_cache(value)
        if record is not None:
            id_result[talent_id] = record
    return name_result, id_result


def _save_cache(db: dict[str, TalentRecord], db_by_id: dict[str, TalentRecord]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(
                {
                    "schema_version": _CACHE_SCHEMA_VERSION,
                    "records": {name: _record_to_cache(record) for name, record in db.items()},
                    "records_by_id": {
                        talent_id: _record_to_cache(record) for talent_id, record in db_by_id.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _record_to_cache(record: TalentRecord) -> dict[str, object]:
    return {
        "description": record.description,
        "icon": record.icon,
        "talent_id": record.talent_id,
        "damage_type": record.damage_type,
        "scaling_family": record.scaling_family,
        "damage_low": record.damage_low,
        "damage_high": record.damage_high,
        "cooldown": record.cooldown,
        "tactical_disable": record.tactical_disable,
        "talent_type": record.talent_type,
        "mode": record.mode,
    }


def _record_from_cache(value: object) -> TalentRecord | None:
    if not isinstance(value, dict):
        return None
    td = value.get("tactical_disable", [])
    return TalentRecord(
        description=str(value.get("description", "")),
        icon=str(value.get("icon", "")),
        talent_id=str(value.get("talent_id", "")),
        damage_type=str(value.get("damage_type", "")),
        scaling_family=str(value.get("scaling_family", "")),
        damage_low=float(value.get("damage_low", 0.0) or 0.0),
        damage_high=float(value.get("damage_high", 0.0) or 0.0),
        cooldown=int(value.get("cooldown", 0) or 0),
        tactical_disable=[str(x) for x in td] if isinstance(td, list) else [],
        talent_type=str(value.get("talent_type", "")),
        mode=str(value.get("mode", "")),
    )
