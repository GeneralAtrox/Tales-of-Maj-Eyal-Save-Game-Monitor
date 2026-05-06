"""
game_data/talent_db.py
----------------------
Parses talent definitions from the ToME game archive (tome.team) and exposes
name- and id-keyed metadata for GUI talent panels and threat scoring.

The database is built lazily on first use and cached as JSON beside this
module, so repeated launches avoid re-scanning the archive unless it changes.

Schema v19: adds talent crit metadata, stat-scaling metadata, improves direct damage type extraction
from projectile/projector calls, keeps both name- and id-keyed records, and prefers direct weapon-hit
multipliers over unrelated helper damage in weapon talents. Also tracks total same-action weapon burst,
engine-default activated talent mode, NPC AI usability, direct numeric resource costs, and simple range metadata.
Weapon talents also track simple ``combatTalentWeaponDamage`` auxiliary talent scaling. Dynamic range metadata can
now point at parsed helper talents when the engine delegates one talent's range to another.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_TOME_TEAM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome.team"
)
_CACHE_FILE = Path(__file__).parent / "_talent_cache.json"
_CACHE_SCHEMA_VERSION = 19
_RESOURCE_COST_FIELDS = frozenset(
    {
        "mana",
        "stamina",
        "vim",
        "positive",
        "negative",
        "hate",
        "psi",
        "soul",
        "steam",
    }
)


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
    """One of ``spell``, ``mind``, ``physical``, ``weapon``, ``stat``, ``flat``, or empty."""
    scaling_stat: str = ""
    """Stat key for ``combatTalentStatDamage`` records (``str``, ``wil``, ...)."""
    scaling_no_dr: bool = False
    """Whether the stat-scaling helper opts out of its old diminishing-return curve."""
    crit_family: str = ""
    """One of ``spell``, ``mind``, ``physical`` when damage is wrapped in a crit helper."""
    damage_low: float = 0.0
    damage_high: float = 0.0
    weapon_burst_low: float = 0.0
    """Sum of direct same-action weapon hit low multipliers. Zero for non-weapon talents."""
    weapon_burst_high: float = 0.0
    """Sum of direct same-action weapon hit high multipliers. Zero for non-weapon talents."""
    weapon_burst_hits: int = 0
    """Number of direct weapon hit calls seen in one activation."""
    weapon_aux_talent_id: str = ""
    """Optional ``T_XXX`` from ``combatTalentWeaponDamage``'s fourth argument."""
    cooldown: int = 0
    tactical_disable: list[str] = field(default_factory=list)
    """Raw tactical-disable tags (``stun``, ``pin``, ``disarm``, ...)."""
    talent_type: str = ""
    """Full category, e.g. ``spell/fire``."""
    mode: str = ""
    """``activated`` | ``sustained`` | ``passive`` (empty if unparsed)."""
    npc_usable: bool = True
    """False when the Lua talent has ``no_npc_use`` set to a truthy value."""
    resource_costs: dict[str, float] = field(default_factory=dict)
    """Direct numeric activated costs keyed by resource short name."""
    requires_target: bool = False
    """True when the Lua talent explicitly sets ``requires_target = true``."""
    target_range: float | None = None
    """Direct numeric ``range`` field when present."""
    target_range_source: str = ""
    """Dynamic range source when static range is unavailable, e.g. ``archery``."""
    target_radius: float = 0.0
    """Direct numeric ``radius`` field when present."""
    numeric_helpers: dict[str, float] = field(default_factory=dict)
    """Simple helper return values, used to resolve delegated range functions."""


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
_RE_NO_NPC_USE_VALUE = re.compile(r"\bno_npc_use\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)")
_RE_RESOURCE_COST = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)\s*,?\s*$")
_RE_REQUIRES_TARGET = re.compile(r"^\s*requires_target\s*=\s*(true|false)\s*,?\s*$")
_RE_DIRECT_NUMERIC_FIELD = re.compile(r"^\s*{field}\s*=\s*(-?\d+(?:\.\d+)?)\s*,?\s*$")
_RE_DIRECT_IDENTIFIER_FIELD = re.compile(r"^\s*{field}\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$")
_RE_TARGET_RETURN_NUMERIC_RANGE = re.compile(
    r"\btarget\s*=\s*function\b[\s\S]*?\breturn\s*\{[\s\S]{0,320}?\brange\s*=\s*(-?\d+(?:\.\d+)?)\b",
    re.MULTILINE,
)
_RE_FUNCTION_RETURN_NUMBER_FIELD = re.compile(
    r"^\s*{field}\s*=\s*function\b[\s\S]*?\breturn\s+(-?\d+(?:\.\d+)?)\b",
    re.MULTILINE,
)
_RE_FUNCTION_SCALE_FIELD = re.compile(
    r"^\s*{field}\s*=\s*function\b[\s\S]*?combatTalent(?:Scale|Limit)\s*"
    r"\(\s*t\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    r"(?:\s*,\s*(-?\d+(?:\.\d+)?))?",
    re.MULTILINE,
)
_RE_FUNCTION_TALENT_RANGE_SOURCE = re.compile(
    r"^\s*{field}\s*=\s*function\b[\s\S]*?getTalentFromId\s*\(\s*self\.(T_[A-Z0-9_]+)\s*\)"
    r"[\s\S]*?\breturn\s+self:getTalentRange\s*\(\s*t\s*\)",
    re.MULTILINE,
)
_RE_FUNCTION_TALENT_HELPER_SOURCE = re.compile(
    r"^\s*{field}\s*=\s*function\b[\s\S]*?\breturn\s+self:callTalent\s*"
    r"\(\s*self\.(T_[A-Z0-9_]+)\s*,\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)",
    re.MULTILINE,
)
_RE_FUNCTION_PERCENT_HELPER_SOURCE = re.compile(
    r"^\s*{field}\s*=\s*function\b[\s\S]*?\blocal\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(-?\d+(?:\.\d+)?)[\s\S]*?1\s*\+\s*(-?\d+(?:\.\d+)?)\s*\*\s*self:callTalent\s*"
    r"\(\s*self\.(T_[A-Z0-9_]+)\s*,\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)"
    r"[\s\S]*?\breturn\s+math\.floor\s*\(\s*\1\s*\*",
    re.MULTILINE,
)
_RE_HELPER_FUNCTION_FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*function\b", re.MULTILINE)
_RE_DAM_DESC = re.compile(r"damDesc\s*\(\s*[^,]+,\s*DamageType[.:](\w+)")
_RE_DAMAGE_TYPE_TOKEN = re.compile(r"DamageType[.:](\w+)")
_RE_SCALING = re.compile(
    r"self:combatTalent(Spell|Mind|Physical|Weapon)Damage"
    r"\s*\(\s*t\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
)
_RE_STAT_SCALING = re.compile(
    r"self:combatTalentStatDamage"
    r"\s*\(\s*t\s*,\s*[\"'](\w+)[\"']\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*(true|false))?"
)
_RE_DIRECT_WEAPON_HIT = re.compile(
    r"(?:attackTarget(?:With)?|archeryShoot)\s*\([\s\S]{0,420}?self:combatTalentWeaponDamage"
    r"\s*\(\s*t\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
    r"(?:\s*,\s*self:getTalentLevel\s*\(\s*self\.(T_[A-Z0-9_]+)\s*\))?"
)
_RE_CRIT_WRAPPER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*:(spellCrit|mindCrit|physicalCrit)\s*\(")
_RE_TACTICAL_DISABLE = re.compile(r"\btactical\s*=\s*\{[^}]*?\bdisable\s*=\s*\{([^}]*)\}", re.DOTALL)
_RE_TACTICAL_KEY = re.compile(r"(\w+)\s*=")
_RE_TABLE_DAMAGE_PAYLOAD = re.compile(r",\s*\{[^}]*\bdam\s*=", re.DOTALL)
_RE_SCALAR_DAMAGE_PAYLOAD = re.compile(
    r",\s*(?:"
    r"(?:self:)?(?:spellCrit|mindCrit|physicalCrit)\s*\("
    r"|rng\.avg\s*\("
    r"|t\.get(?:Damage|Dam)\s*\("
    r"|self:combatTalent(?:Spell|Mind|Physical)Damage\s*\("
    r"|\bdam\b"
    r"|\bdamage\b"
    r")",
    re.DOTALL,
)
_BASE_DAMAGE_TYPES = {
    "ACID",
    "ARCANE",
    "BLIGHT",
    "COLD",
    "DARKNESS",
    "FIRE",
    "LIGHT",
    "LIGHTNING",
    "MIND",
    "NATURE",
    "PHYSICAL",
    "STEAM",
    "TEMPORAL",
}
_DAMAGE_TYPE_ALIASES = {
    "BLEED": "PHYSICAL",
    "BLIGHT_POISON": "BLIGHT",
    "BOUNCE_SLIME": "NATURE",
    "CRIPPLING_POISON": "NATURE",
    "HALLUCINOGENIC_MOSS": "NATURE",
    "INSIDIOUS_POISON": "NATURE",
    "MUCUS": "NATURE",
    "NOURISHING_MOSS": "NATURE",
    "PHYSICALBLEED": "PHYSICAL",
    "POISON": "NATURE",
    "RANDOM_POISON": "NATURE",
    "SLIME": "NATURE",
    "SLIPPERY_MOSS": "NATURE",
    "SPYDRIC_POISON": "NATURE",
}


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


def resolve_target_range(
    record: TalentRecord,
    records: Mapping[str, TalentRecord] | None = None,
    *,
    weapon_range: float = 0.0,
    _seen: set[str] | None = None,
) -> float | None:
    if record.target_range is not None:
        return record.target_range
    source = record.target_range_source
    if not source:
        return None
    if source == "archery":
        return weapon_range if weapon_range > 0.0 else None
    if records is None:
        return None
    if _seen is None:
        _seen = set()
    if record.talent_id:
        if record.talent_id in _seen:
            return None
        _seen.add(record.talent_id)
    if source.startswith("talent_range:"):
        _, talent_id = source.split(":", 1)
        other = records.get(talent_id)
        if other is None:
            return None
        return resolve_target_range(other, records, weapon_range=weapon_range, _seen=_seen)
    if source.startswith("talent_helper:"):
        parts = source.split(":")
        if len(parts) != 3:
            return None
        _, talent_id, helper_name = parts
        return _helper_value(records, talent_id, helper_name)
    if source.startswith("talent_helper_pct:"):
        parts = source.split(":")
        if len(parts) != 5:
            return None
        _, talent_id, helper_name, raw_base, raw_percent = parts
        helper_value = _helper_value(records, talent_id, helper_name)
        base = _float_or_none(raw_base)
        percent = _float_or_none(raw_percent)
        if helper_value is None or base is None or percent is None:
            return None
        return float(math.ceil(base * (1.0 + percent * helper_value)))
    return None


def _helper_value(records: Mapping[str, TalentRecord], talent_id: str, helper_name: str) -> float | None:
    record = records.get(talent_id)
    if record is None:
        return None
    return record.numeric_helpers.get(helper_name)


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
            mode=_extract_mode(block),
            cooldown=_extract_cooldown(block),
            tactical_disable=_extract_tactical_disable(block),
            npc_usable=_extract_npc_usable(block),
            resource_costs=_extract_resource_costs(block),
            requires_target=_extract_requires_target(block),
            target_range=_extract_target_range(block),
            target_range_source=_extract_target_range_source(block),
            target_radius=_extract_numeric_or_scaled_field(block, "radius") or 0.0,
            numeric_helpers=_extract_numeric_helpers(block),
        )
        dtype, family, stat, no_dr, low, high, burst_low, burst_high, burst_hits, aux_talent_id = _extract_damage(
            block
        )
        record.damage_type = dtype
        record.scaling_family = family
        record.scaling_stat = stat
        record.scaling_no_dr = no_dr
        record.crit_family = _extract_crit_family(block)
        record.damage_low = low
        record.damage_high = high
        record.weapon_burst_low = burst_low
        record.weapon_burst_high = burst_high
        record.weapon_burst_hits = burst_hits
        record.weapon_aux_talent_id = aux_talent_id
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


def _extract_mode(block: str) -> str:
    return _extract_first(block, _RE_MODE) or "activated"


def _extract_cooldown(block: str) -> int:
    m = _RE_COOLDOWN.search(block)
    return int(m.group(1)) if m else 0


def _extract_npc_usable(block: str) -> bool:
    if not (match := _RE_NO_NPC_USE_VALUE.search(_strip_lua_comments(block))):
        return True
    return match.group(1).lower() in {"false", "nil"}


def _extract_resource_costs(block: str) -> dict[str, float]:
    costs: dict[str, float] = {}
    for line in _strip_lua_comments(block).splitlines():
        if not (match := _RE_RESOURCE_COST.match(line)):
            continue
        resource = match.group(1).lower()
        if resource not in _RESOURCE_COST_FIELDS:
            continue
        try:
            cost = float(match.group(2))
        except ValueError:
            continue
        if cost > 0:
            costs[resource] = cost
    return costs


def _extract_requires_target(block: str) -> bool:
    for line in _strip_lua_comments(block).splitlines():
        if match := _RE_REQUIRES_TARGET.match(line):
            return match.group(1) == "true"
    return False


def _extract_target_range(block: str) -> float | None:
    value = _extract_numeric_or_scaled_field(block, "range")
    if value is not None:
        return value
    if _extract_direct_identifier_field(block, "range") == "trap_range":
        return 10.0
    value = _extract_target_table_range(block)
    if value is not None:
        return value
    if _extract_requires_target(block) and not _has_direct_field_assignment(block, "range"):
        return 1.0
    return None


def _extract_target_range_source(block: str) -> str:
    if (
        _extract_numeric_or_scaled_field(block, "range") is not None
        or _extract_direct_identifier_field(block, "range") == "trap_range"
        or _extract_target_table_range(block) is not None
    ):
        return ""
    identifier = _extract_direct_identifier_field(block, "range")
    if identifier == "archery_range":
        return "archery"
    if source := _extract_percent_helper_range_source(block, "range"):
        return source
    if source := _extract_talent_helper_range_source(block, "range"):
        return source
    if source := _extract_talent_range_source(block, "range"):
        return source
    return ""


def _extract_target_table_range(block: str) -> float | None:
    if not (match := _RE_TARGET_RETURN_NUMERIC_RANGE.search(_strip_lua_comments(block))):
        return None
    return _float_or_none(match.group(1))


def _extract_talent_range_source(block: str, field_name: str) -> str:
    pattern = re.compile(_RE_FUNCTION_TALENT_RANGE_SOURCE.pattern.format(field=re.escape(field_name)), re.MULTILINE)
    if not (match := pattern.search(_strip_lua_comments(block))):
        return ""
    return f"talent_range:{match.group(1)}"


def _extract_talent_helper_range_source(block: str, field_name: str) -> str:
    pattern = re.compile(_RE_FUNCTION_TALENT_HELPER_SOURCE.pattern.format(field=re.escape(field_name)), re.MULTILINE)
    if not (match := pattern.search(_strip_lua_comments(block))):
        return ""
    return f"talent_helper:{match.group(1)}:{match.group(2)}"


def _extract_percent_helper_range_source(block: str, field_name: str) -> str:
    pattern = re.compile(_RE_FUNCTION_PERCENT_HELPER_SOURCE.pattern.format(field=re.escape(field_name)), re.MULTILINE)
    if not (match := pattern.search(_strip_lua_comments(block))):
        return ""
    _variable, base, percent, talent_id, helper_name = match.groups()
    return f"talent_helper_pct:{talent_id}:{helper_name}:{base}:{percent}"


def _extract_direct_numeric_field(block: str, field_name: str) -> float | None:
    pattern = re.compile(_RE_DIRECT_NUMERIC_FIELD.pattern.format(field=re.escape(field_name)))
    for line in _strip_lua_comments(block).splitlines():
        if not (match := pattern.match(line)):
            continue
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _extract_direct_identifier_field(block: str, field_name: str) -> str:
    pattern = re.compile(_RE_DIRECT_IDENTIFIER_FIELD.pattern.format(field=re.escape(field_name)))
    for line in _strip_lua_comments(block).splitlines():
        if match := pattern.match(line):
            return match.group(1)
    return ""


def _has_direct_field_assignment(block: str, field_name: str) -> bool:
    pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*=", re.MULTILINE)
    return pattern.search(_strip_lua_comments(block)) is not None


def _extract_numeric_or_scaled_field(block: str, field_name: str) -> float | None:
    if (value := _extract_direct_numeric_field(block, field_name)) is not None:
        return value
    escaped = re.escape(field_name)
    if match := re.search(_RE_FUNCTION_SCALE_FIELD.pattern.format(field=escaped), block, re.MULTILINE):
        values = [_float_or_none(group) for group in match.groups()]
        numeric_values = [value for value in values if value is not None]
        if numeric_values:
            return max(numeric_values)
    if match := re.search(_RE_FUNCTION_RETURN_NUMBER_FIELD.pattern.format(field=escaped), block, re.MULTILINE):
        return _float_or_none(match.group(1))
    return None


def _extract_numeric_helpers(block: str) -> dict[str, float]:
    helpers: dict[str, float] = {}
    stripped = _strip_lua_comments(block)
    for match in _RE_HELPER_FUNCTION_FIELD.finditer(stripped):
        helper_name = match.group(1)
        if helper_name not in {"getRange", "rangebonus"} and not helper_name.lower().endswith("range"):
            continue
        value = _extract_numeric_or_scaled_field(stripped, helper_name)
        if value is not None:
            helpers[helper_name] = value
    return helpers


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _strip_lua_comments(text: str) -> str:
    text = re.sub(r"--\[(=*)\[[\s\S]*?\]\1\]", "", text)
    return re.sub(r"--[^\n]*", "", text)


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


def _extract_damage(block: str) -> tuple[str, str, str, bool, float, float, float, float, int, str]:
    dtype = ""
    if m := _RE_DAM_DESC.search(block):
        dtype = _normalize_damage_type(m.group(1))
    family = ""
    stat = ""
    no_dr = False
    low = 0.0
    high = 0.0
    burst_low = 0.0
    burst_high = 0.0
    burst_hits = 0
    aux_talent_id = ""
    if weapon_hits := _direct_weapon_hits(block):
        family = "weapon"
        low, high, aux_talent_id = max(weapon_hits, key=lambda hit: hit[1])
        burst_low = sum(hit[0] for hit in weapon_hits)
        burst_high = sum(hit[1] for hit in weapon_hits)
        burst_hits = len(weapon_hits)
    elif m := _RE_SCALING.search(block):
        family = m.group(1).lower()  # spell|mind|physical|weapon
        try:
            low = float(m.group(2))
            high = float(m.group(3))
        except ValueError:
            low = high = 0.0
    elif m := _RE_STAT_SCALING.search(block):
        family = "stat"
        stat = m.group(1).lower()
        no_dr = m.group(4) == "true"
        try:
            low = float(m.group(2))
            high = float(m.group(3))
        except ValueError:
            low = high = 0.0
    if family == "weapon" and high > 0.0 and burst_high <= 0.0:
        burst_low = low
        burst_high = high
        burst_hits = 1
    if not dtype:
        dtype = _extract_direct_damage_type(block)
    return dtype, family, stat, no_dr, low, high, burst_low, burst_high, burst_hits, aux_talent_id


def _direct_weapon_hits(block: str) -> list[tuple[float, float, str]]:
    hits: list[tuple[float, float, str]] = []
    for m in _RE_DIRECT_WEAPON_HIT.finditer(block):
        try:
            hits.append((float(m.group(1)), float(m.group(2)), m.group(3) or ""))
        except ValueError:
            continue
    return hits


def _extract_direct_damage_type(block: str) -> str:
    for match in _RE_DAMAGE_TYPE_TOKEN.finditer(block):
        before = block[max(0, match.start() - 90) : match.start()]
        after = block[match.end() : match.end() + 240]
        if "damDesc" in before:
            continue
        if "project" not in before and "project" not in after[:90]:
            continue
        if _RE_TABLE_DAMAGE_PAYLOAD.search(after) or _RE_SCALAR_DAMAGE_PAYLOAD.search(after):
            return _normalize_damage_type(match.group(1))
    return ""


def _extract_crit_family(block: str) -> str:
    if not (match := _RE_CRIT_WRAPPER.search(block)):
        return ""
    wrapper = match.group(1)
    if wrapper == "spellCrit":
        return "spell"
    if wrapper == "mindCrit":
        return "mind"
    if wrapper == "physicalCrit":
        return "physical"
    return ""


def _normalize_damage_type(raw: str) -> str:
    damage_type = raw.strip().upper()
    if damage_type in _BASE_DAMAGE_TYPES:
        return damage_type
    if damage_type in _DAMAGE_TYPE_ALIASES:
        return _DAMAGE_TYPE_ALIASES[damage_type]
    if damage_type == "ICE" or damage_type.startswith("COLD") or damage_type == "MINDFREEZE":
        return "COLD"
    if damage_type.startswith("FIRE"):
        return "FIRE"
    if damage_type.startswith("LIGHTNING"):
        return "LIGHTNING"
    if damage_type.startswith("ACID"):
        return "ACID"
    if damage_type.startswith("MIND"):
        return "MIND"
    if damage_type.startswith("PHYSICAL"):
        return "PHYSICAL"
    if damage_type.startswith("BLIGHT"):
        return "BLIGHT"
    if damage_type.startswith("DARK"):
        return "DARKNESS"
    if damage_type.startswith("LIGHT"):
        return "LIGHT"
    if damage_type.startswith("TEMPORAL"):
        return "TEMPORAL"
    if damage_type.startswith("NATURE"):
        return "NATURE"
    if damage_type.startswith("ARCANE"):
        return "ARCANE"
    return ""


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
        "scaling_stat": record.scaling_stat,
        "scaling_no_dr": record.scaling_no_dr,
        "crit_family": record.crit_family,
        "damage_low": record.damage_low,
        "damage_high": record.damage_high,
        "weapon_burst_low": record.weapon_burst_low,
        "weapon_burst_high": record.weapon_burst_high,
        "weapon_burst_hits": record.weapon_burst_hits,
        "weapon_aux_talent_id": record.weapon_aux_talent_id,
        "cooldown": record.cooldown,
        "tactical_disable": record.tactical_disable,
        "talent_type": record.talent_type,
        "mode": record.mode,
        "npc_usable": record.npc_usable,
        "resource_costs": dict(record.resource_costs),
        "requires_target": record.requires_target,
        "target_range": record.target_range,
        "target_range_source": record.target_range_source,
        "target_radius": record.target_radius,
        "numeric_helpers": dict(record.numeric_helpers),
    }


def _record_from_cache(value: object) -> TalentRecord | None:
    if not isinstance(value, dict):
        return None
    td = value.get("tactical_disable", [])
    raw_costs = value.get("resource_costs", {})
    cost_items = raw_costs.items() if isinstance(raw_costs, dict) else ()
    raw_helpers = value.get("numeric_helpers", {})
    helper_items = raw_helpers.items() if isinstance(raw_helpers, dict) else ()
    return TalentRecord(
        description=str(value.get("description", "")),
        icon=str(value.get("icon", "")),
        talent_id=str(value.get("talent_id", "")),
        damage_type=str(value.get("damage_type", "")),
        scaling_family=str(value.get("scaling_family", "")),
        scaling_stat=str(value.get("scaling_stat", "")),
        scaling_no_dr=bool(value.get("scaling_no_dr", False)),
        crit_family=str(value.get("crit_family", "")),
        damage_low=float(value.get("damage_low", 0.0) or 0.0),
        damage_high=float(value.get("damage_high", 0.0) or 0.0),
        weapon_burst_low=float(value.get("weapon_burst_low", 0.0) or 0.0),
        weapon_burst_high=float(value.get("weapon_burst_high", 0.0) or 0.0),
        weapon_burst_hits=int(value.get("weapon_burst_hits", 0) or 0),
        weapon_aux_talent_id=str(value.get("weapon_aux_talent_id", "")),
        cooldown=int(value.get("cooldown", 0) or 0),
        tactical_disable=[str(x) for x in td] if isinstance(td, list) else [],
        talent_type=str(value.get("talent_type", "")),
        mode=str(value.get("mode", "")),
        npc_usable=bool(value.get("npc_usable", True)),
        resource_costs={
            str(key): float(cost)
            for key, cost in cost_items
            if isinstance(cost, (int, float))
        },
        requires_target=bool(value.get("requires_target", False)),
        target_range=(
            float(value["target_range"])
            if isinstance(value.get("target_range"), (int, float))
            else None
        ),
        target_range_source=str(value.get("target_range_source", "")),
        target_radius=float(value.get("target_radius", 0.0) or 0.0),
        numeric_helpers={
            str(key): float(helper)
            for key, helper in helper_items
            if isinstance(helper, (int, float))
        },
    )
