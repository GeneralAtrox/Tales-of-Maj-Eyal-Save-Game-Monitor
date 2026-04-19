"""
game_data/talent_db.py
----------------------
Parses talent definitions from the ToME game archive (tome.team) and exposes
name- and id-keyed metadata for GUI talent panels and threat scoring.

The database is built lazily on first use and cached as JSON beside this
module, so repeated launches avoid re-scanning the archive unless it changes.

Schema v3: adds damage metadata (talent_id, damage_type, scaling_family,
damage_low/high, cooldown, tactical_disable) used by `scoring.talent_threat`.
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
_CACHE_SCHEMA_VERSION = 3


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
    _db = _load_or_build()
    _db_by_id = {r.talent_id: r for r in _db.values() if r.talent_id}


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
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        td = value.get("tactical_disable", [])
        result[name] = TalentRecord(
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
    return result


def _save_cache(db: dict[str, TalentRecord]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(
                {
                    "schema_version": _CACHE_SCHEMA_VERSION,
                    "records": {
                        name: {
                            "description": r.description,
                            "icon": r.icon,
                            "talent_id": r.talent_id,
                            "damage_type": r.damage_type,
                            "scaling_family": r.scaling_family,
                            "damage_low": r.damage_low,
                            "damage_high": r.damage_high,
                            "cooldown": r.cooldown,
                            "tactical_disable": r.tactical_disable,
                            "talent_type": r.talent_type,
                            "mode": r.mode,
                        }
                        for name, r in db.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
