from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from game_data.lua_extractor import RE_NAME, find_tome_team, iter_balanced_blocks
from scoring import combat_math as cm
from scoring.ranks import rank_label

_BASE_TEMPLATE_RE = re.compile(r'define_as\s*=\s*"BASE_')
_DEFINE_AS_RE = re.compile(r'\bdefine_as\s*=\s*"([^"]+)"')
_TYPE_RE = re.compile(r'\btype\s*=\s*"([^"]+)"')
_SUBTYPE_RE = re.compile(r'\bsubtype\s*=\s*"([^"]+)"')
_FACTION_RE = re.compile(r'\bfaction\s*=\s*"([^"]+)"')
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDORED_DATA_ROOT = _REPO_ROOT / "tools" / "t-engine4-master" / "game" / "modules" / "tome" / "data"


@dataclass(frozen=True, slots=True)
class BossTemplate:
    name: str
    location: str
    level_label: str
    quest: str = ""
    source_names: tuple[str, ...] = ()
    source_zone: str = ""

    @property
    def key(self) -> str:
        return f"{_slugify(self.name)}::{_slugify(self.location)}"

    @property
    def display_label(self) -> str:
        parts = [self.name, self.location, self.level_label]
        if self.quest:
            parts.append(self.quest)
        return ", ".join(parts)

    @property
    def candidate_names(self) -> tuple[str, ...]:
        return self.source_names or (self.name,)


@dataclass(slots=True)
class BossTemplateStats:
    template: BossTemplate
    name: str
    level: float
    max_life: float
    rank: float
    rank_name: str
    faction: str
    type_name: str
    subtype: str
    global_speed: float
    dam: float
    atk: float
    apr: float
    crit_chance_pct: float
    crit_power_bonus_pct: float
    physspeed: float
    damage_type: str
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    has_combat_data: bool = False

    @property
    def warning(self) -> str:
        if self.has_combat_data:
            return ""
        return "Combat defaults are partial; adjust damage and attack fields."


@dataclass(frozen=True, slots=True)
class _BossBlock:
    source_path: str
    block: str


@dataclass(frozen=True, slots=True)
class BossActorRef:
    source_path: str
    name: str
    define_as: str = ""


_BOSS_TEMPLATES: tuple[BossTemplate, ...] = (
    BossTemplate("Prox the Mighty", "Trollmire", "7+", "Quest: Of trolls and damp caves"),
    BossTemplate("Shax the Slimy", "Trollmire (alternative)", "7+", "Quest: Of trolls and damp caves"),
    BossTemplate("Bill the Stone Troll", "Trollmire", "7+", "Optional Quest: Hidden Treasure"),
    BossTemplate("The Shade", "Ruins of Kor'Pul", "7+", "Quest: Of trolls and damp caves"),
    BossTemplate("The Possessed", "Ruins of Kor'Pul (alternative)", "7+", "Quest: Of trolls and damp caves"),
    BossTemplate("Norgos the Guardian", "Norgos' Lair", "7+", "Quest: Madness of the Ages"),
    BossTemplate("Norgos the Frozen", "Norgos' Lair (alternative)", "7+", "Quest: Madness of the Ages"),
    BossTemplate("The Withering Thing", "Heart of the Gloom", "7+", "Quest: Madness of the Ages"),
    BossTemplate("The Dreaming One", "Heart of the Gloom (alternative)", "7+", "Quest: Madness of the Ages"),
    BossTemplate("Spellblaze Crystal", "Scintillating Caves", "7+", "Quest: Echoes of the Spellblaze"),
    BossTemplate("Rhaloren Inquisitor", "Rhaloren Camp", "7+", "Quest: Echoes of the Spellblaze"),
    BossTemplate("Brotoq the Reaver", "Escape from Reknor", "7+", "Quest: Reknor is lost!"),
    BossTemplate("The Mouth", "The Deep Bellow", "7+", "Quest: From bellow, it devours"),
    BossTemplate("Murgol, the Yaech Lord", "Murgol Lair", "7+", "Quest: Following The Way"),
    BossTemplate("Lady Nashva the Streambender", "Murgol Lair (alternative)", "7+", "Quest: Following The Way"),
    BossTemplate("Ritch Great Hive Mother", "Ritches Tunnels", "7+", "Quest: Following The Way"),
    BossTemplate("Half-Finished Bone Giant", "Blighted Ruins", "7+", "Quest: The rotting stench of the dead"),
    BossTemplate("Spacial Disturbance", "Abashed Expanse", "7+", "Quest: Spellblaze Fallouts"),
    BossTemplate("Lady Zoisla the Tidebringer", "Slazish Fens", "7+", "Quest: Serpentine Invaders"),
    BossTemplate("Weaver Queen", "Unhallowed Morass", "7+", "Quest: Future Echoes"),
    BossTemplate("Assassin Lord", "Unknown tunnels", "8+", "Optional Quest: Trapped!"),
    BossTemplate(
        "Ben Cruthdar, the Cursed Lumberjack",
        "Village",
        "10+",
        "Optional Quest: The Beast Within",
        source_names=("Ben Cruthdar, the Cursed",),
    ),
    BossTemplate("Subject Z", "Ruined Halfling Complex", "10+", "Can unlock Yeek, then later Mindslayer"),
    BossTemplate("Wrathroot", "Old Forest", "12+", "Quest: Into the darkness"),
    BossTemplate("Shardskin", "Old Forest (alternative)", "12+", "Quest: Into the darkness"),
    BossTemplate("Minotaur of the Labyrinth", "The Maze", "12+", "Quest: Into the darkness"),
    BossTemplate("Horned Horror", "The Maze (alternative)", "12+", "Quest: Into the darkness"),
    BossTemplate("Rantha the Worm", "Daikara", "12+", "Quest: Into the darkness"),
    BossTemplate("Varsha the Writhing", "Daikara (alternative)", "12+", "Quest: Into the darkness"),
    BossTemplate("Blood Master", "Hidden Compound", "14+", "Optional Quest: Till the Blood Runs Clear"),
    BossTemplate("Sandworm Queen", "Sandworm Lair", "15+", "Quest: Into the darkness"),
    BossTemplate("Urkis, the High Tempest", "Tempest Peak", "17+", "Optional Quest: Storming the city"),
    BossTemplate("Weirdling Beast", "Sher'Tul Fortress", "19+", "Optional Quest: Sher'Tul Fortress"),
    BossTemplate(
        "Chronolith Twin & Chronolith Clone",
        "Temporal Rift",
        "20+",
        "Optional Quest: Back and Back and Back To The Future",
        source_names=("Chronolith Twin", "Chronolith Clone"),
        source_zone="temporal-rift",
    ),
    BossTemplate("Celia", "Last Hope Graveyard", "20+", "Optional Quest: And now for a grave"),
    BossTemplate("The Master", "Dreadfell", "23+", "Quest: The Island of Dread"),
    BossTemplate("Healer Astelrid", "Old Conclave Vault", "23+", "Unlocks Ogre"),
    BossTemplate(
        "Grand Corruptor",
        "Mark of the Spellblaze",
        "25+",
        "Optional Quest: The fall of Zigur",
        source_zone="mark-spellblaze",
    ),
    BossTemplate("Mindworm", "Dogroth Caldera", "25+", "Connected to Solipsist unlock", source_zone="noxious-caldera"),
    BossTemplate("Greater Mummy Lord", "Elven Ruins", "30+"),
    BossTemplate("Corrupted Oozemancer", "Sludgenest", "35+", "Unlocks Oozemancer"),
    BossTemplate("Golbug the Destroyer", "Reknor", "28+", "Quest: Let's hunt some Orc"),
    BossTemplate("Krogar", "Unremarkable Cave", "25+", "Quest: Strange New World"),
    BossTemplate("Ungol\u00eb", "Ardhungol", "30+", "Optional Quest: Eight legs of wonder"),
    BossTemplate("Ukllmswwik the Wise", "Flooded Cave", "30+", "Optional Quest: The Temple of Creation"),
    BossTemplate("Slasul", "Temple of Creation", "30+", "Optional Quest: The Temple of Creation"),
    BossTemplate("Warmaster Gnarg", "Vor Armoury", "35+", "Optional Quest: There and back again"),
    BossTemplate(
        "Briagh, the Great Sand Wyrm",
        "Briagh's Lair",
        "35+",
        "Optional Quest: There and back again",
        source_names=("Briagh, Great Sand Wyrm",),
    ),
    BossTemplate("Rak'Shor Cultist", "Shadow Crypt", "35+", "Unlocks Doomed"),
    BossTemplate(
        "Shade Of Telos",
        "Ruins of Telmur",
        "38+",
        "Optional Quest: Back and there again",
        source_names=("The Shade of Telos",),
    ),
    BossTemplate("Draebor, the Imp", "Fearscape (Tannen)", "35+", "Optional Quest: Back and there again"),
    BossTemplate(
        "Tannen & Drolem",
        "Tannen's Tower",
        "35+",
        "Optional Quest: Back and there again",
        source_names=("Tannen", "Drolem"),
        source_zone="tannen-tower",
    ),
    BossTemplate("Fyrk, Faeros High Guard", "Charred Scar", "35+", "Quest: The Doom of the World!"),
    BossTemplate(
        "Rak'Shor, Grand Necromancer of the Pride",
        "Rak'Shor Pride",
        "35+",
        "Quest: The many Prides of the Orcs",
        source_zone="rak-shor-pride",
    ),
    BossTemplate("Corrupted Daelach", "Ithilthum, Valley of the Moon", "40+", "Optional Quest: Lost Knowledge"),
    BossTemplate(
        "Vor, the Grand Geomancer",
        "Vor Pride",
        "40+",
        "Quest: The many Prides of the Orcs",
        source_names=("Vor, Grand Geomancer of the Pride",),
        source_zone="vor-pride",
    ),
    BossTemplate(
        "Gorbat, Supreme Wyrmic of the Pride",
        "Gorbat Pride",
        "40+",
        "Quest: The many Prides of the Orcs",
        source_zone="gorbat-pride",
    ),
    BossTemplate(
        "Grushnak, Battlemaster of the Pride",
        "Grushnak Pride",
        "45+",
        "Quest: The many Prides of the Orcs",
        source_zone="grushnak-pride",
    ),
    BossTemplate(
        "Elandar & Argoniel",
        "High Peak",
        "75+",
        "Quest: Falling Toward Apotheosis",
        source_names=("Elandar", "Argoniel"),
        source_zone="high-peak",
    ),
)


def get_boss_templates() -> tuple[BossTemplate, ...]:
    return _BOSS_TEMPLATES


def get_boss_template(key: str) -> BossTemplate | None:
    return _boss_template_index().get(key)


def load_boss_template_stats(key: str) -> BossTemplateStats | None:
    template = get_boss_template(key)
    if template is None:
        return None
    return _boss_template_stats(template)


def load_boss_actor_refs(key: str) -> tuple[BossActorRef, ...] | None:
    template = get_boss_template(key)
    if template is None:
        return None
    refs = _boss_actor_refs(template)
    return refs or None


@lru_cache(maxsize=1)
def _boss_template_index() -> dict[str, BossTemplate]:
    return {template.key: template for template in _BOSS_TEMPLATES}


@lru_cache(maxsize=None)
def _boss_template_stats(template: BossTemplate) -> BossTemplateStats:
    boss_block = _resolve_boss_block(template)
    if boss_block is None:
        return BossTemplateStats(
            template=template,
            name=template.name,
            level=_template_level(template.level_label),
            max_life=0.0,
            rank=4.0,
            rank_name=rank_label(4.0),
            faction="",
            type_name="",
            subtype="",
            global_speed=1.0,
            dam=0.0,
            atk=0.0,
            apr=0.0,
            crit_chance_pct=0.0,
            crit_power_bonus_pct=0.0,
            physspeed=1.0,
            damage_type="PHYSICAL",
        )

    block = boss_block.block
    combat_block = _extract_table(block, "combat")
    inc_damage = _parse_damage_table(_extract_table(block, "inc_damage"))
    resists_pen = _parse_damage_table(_extract_table(block, "resists_pen"))
    weapon_dam = _combat_value(combat_block, "dam")
    stats = _parse_number_table(_extract_table(block, "stats"))
    dammod = _parse_number_table(_extract_table(combat_block or "", "dammod"))
    dam = _estimate_template_damage(
        weapon_dam,
        combat_dam=_parse_scalar_field(block, "combat_dam"),
        stats=stats,
        dammod=dammod,
    )
    atk = _combat_value(combat_block, "atk")
    apr = _combat_value(combat_block, "apr")
    crit = (
        _parse_scalar_field(block, "combat_physcrit")
        + _parse_scalar_field(block, "combat_generic_crit")
        + _combat_value(combat_block, "physcrit")
    )
    if crit == 0.0:
        crit = _combat_value(combat_block, "crit")
    crit_power = _parse_scalar_field(block, "combat_critical_power") + _combat_value(combat_block, "crit_power")
    physspeed = _combat_value(combat_block, "physspeed")
    if physspeed == 0.0:
        physspeed = _parse_scalar_field(block, "combat_physspeed", default=1.0)
    damage_type = _parse_damage_type_expr(_extract_value_expr(combat_block or "", "damtype"))

    has_combat_data = any(
        (
            combat_block is not None,
            dam > 0.0,
            atk > 0.0,
            apr > 0.0,
            crit > 0.0,
            crit_power > 0.0,
            physspeed not in (0.0, 1.0),
        )
    )

    rank = _parse_scalar_field(block, "rank", default=4.0)
    return BossTemplateStats(
        template=template,
        name=template.name,
        level=_parse_scalar_field(block, "level_range", default=_template_level(template.level_label)),
        max_life=_parse_scalar_field(block, "max_life"),
        rank=rank,
        rank_name=rank_label(rank),
        faction=_parse_string(block, _FACTION_RE),
        type_name=_parse_string(block, _TYPE_RE),
        subtype=_parse_string(block, _SUBTYPE_RE),
        global_speed=_parse_scalar_field(
            block,
            "global_speed_base",
            default=_parse_scalar_field(block, "global_speed", default=1.0),
        )
        or 1.0,
        dam=dam,
        atk=atk,
        apr=apr,
        crit_chance_pct=crit,
        crit_power_bonus_pct=crit_power,
        physspeed=physspeed or 1.0,
        damage_type=damage_type,
        inc_damage=inc_damage,
        resists_pen=resists_pen,
        has_combat_data=has_combat_data,
    )


@lru_cache(maxsize=None)
def _boss_actor_refs(template: BossTemplate) -> tuple[BossActorRef, ...]:
    refs: list[BossActorRef] = []
    for candidate_name in template.candidate_names:
        boss_block = _resolve_boss_block_for_name(template, candidate_name)
        if boss_block is None:
            continue
        define_as_match = _DEFINE_AS_RE.search(boss_block.block)
        refs.append(
            BossActorRef(
                source_path=_normalize_source_path(boss_block.source_path),
                name=candidate_name,
                define_as=define_as_match.group(1).strip() if define_as_match else "",
            )
        )
    return tuple(refs)


@lru_cache(maxsize=1)
def _boss_block_map() -> dict[str, list[_BossBlock]]:
    boss_map: dict[str, list[_BossBlock]] = {}
    for source_path, lua in _iter_npc_sources():
        for block in iter_balanced_blocks(lua, "newEntity"):
            if _BASE_TEMPLATE_RE.search(block):
                continue
            name_match = RE_NAME.search(block)
            if name_match is None:
                continue
            normalized = _normalize_name(name_match.group(1))
            boss_map.setdefault(normalized, []).append(_BossBlock(source_path=source_path, block=block))
    return boss_map


def _resolve_boss_block(template: BossTemplate) -> _BossBlock | None:
    for candidate_name in template.candidate_names:
        boss_block = _resolve_boss_block_for_name(template, candidate_name)
        if boss_block is not None:
            return boss_block
    return None


def _resolve_boss_block_for_name(template: BossTemplate, candidate_name: str) -> _BossBlock | None:
    boss_map = _boss_block_map()
    entries = boss_map.get(_normalize_name(candidate_name), [])
    if not entries:
        return None
    if template.source_zone:
        zone_match = f"/zones/{template.source_zone}/"
        filtered = [entry for entry in entries if zone_match in entry.source_path.replace("\\", "/")]
        if filtered:
            return filtered[-1]
    return entries[-1]


def _iter_npc_sources() -> list[tuple[str, str]]:
    tome_team = find_tome_team()
    if tome_team and tome_team.is_file():
        try:
            with zipfile.ZipFile(tome_team) as zf:
                names = zf.namelist()
                generic_paths = sorted(
                    name for name in names if name.startswith("data/general/npcs/") and name.endswith(".lua")
                )
                zone_paths = sorted(
                    name for name in names if re.match(r"data/zones/[^/]+/npcs\.lua", name)
                )
                return [
                    (path, zf.read(path).decode("utf-8", errors="replace"))
                    for path in generic_paths + zone_paths
                ]
        except Exception:  # noqa: BLE001
            pass

    if _VENDORED_DATA_ROOT.exists():
        generic_paths = sorted((_VENDORED_DATA_ROOT / "general" / "npcs").glob("*.lua"))
        zone_paths = sorted((_VENDORED_DATA_ROOT / "zones").glob("*/npcs.lua"))
        sources: list[tuple[str, str]] = []
        for path in generic_paths + zone_paths:
            try:
                sources.append((path.as_posix(), path.read_text(encoding="utf-8", errors="replace")))
            except Exception:  # noqa: BLE001
                continue
        return sources

    return []


def _parse_string(block: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(block)
    if match is None:
        return ""
    return match.group(1).strip()


def _parse_scalar_field(block: str, field_name: str, default: float = 0.0) -> float:
    expr = _extract_value_expr(block, field_name)
    if expr is None:
        return default
    return _parse_number_expr(expr, default=default)


def _combat_value(combat_block: str | None, field_name: str) -> float:
    if not combat_block:
        return 0.0
    expr = _extract_value_expr(combat_block, field_name)
    if expr is None:
        return 0.0
    return _parse_number_expr(expr)


def _parse_number_expr(expr: str, default: float = 0.0) -> float:
    cleaned = expr.strip()
    if not cleaned or cleaned == "nil":
        return default
    if cleaned.startswith("(") and cleaned.endswith(")"):
        return _parse_number_expr(cleaned[1:-1], default=default)

    try:
        return float(cleaned)
    except ValueError:
        pass

    if "resolvers.rngavg" in cleaned:
        args = _extract_call_args(cleaned, "resolvers.rngavg")
        if len(args) >= 2:
            left = _parse_number_expr(args[0], default=default)
            right = _parse_number_expr(args[1], default=left)
            return (left + right) / 2.0

    if "resolvers.levelup" in cleaned:
        args = _extract_call_args(cleaned, "resolvers.levelup")
        if args:
            return _parse_number_expr(args[0], default=default)

    if "resolvers.mbonus" in cleaned:
        args = _extract_call_args(cleaned, "resolvers.mbonus")
        if args:
            return _parse_number_expr(args[0], default=default)

    if cleaned.startswith("math.max"):
        args = _extract_call_args(cleaned, "math.max")
        if args:
            return max(_parse_number_expr(arg, default=default) for arg in args)

    if cleaned.startswith("math.min"):
        args = _extract_call_args(cleaned, "math.min")
        if args:
            return min(_parse_number_expr(arg, default=default) for arg in args)

    number_match = _NUMBER_RE.search(cleaned)
    if number_match is not None:
        try:
            return float(number_match.group(0))
        except ValueError:
            return default
    return default


def _extract_call_args(expr: str, call_name: str) -> list[str]:
    start = expr.find(call_name)
    if start < 0:
        return []
    open_index = expr.find("(", start)
    if open_index < 0:
        return []
    depth = 0
    current: list[str] = []
    args: list[str] = []
    for char in expr[open_index + 1 :]:
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            if depth == 0:
                arg = "".join(current).strip()
                if arg:
                    args.append(arg)
                break
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return args


def _extract_table(block: str, field_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(field_name)}\s*=\s*\{{", block)
    if match is None:
        return None
    start = match.end() - 1
    depth = 0
    for index in range(start, len(block)):
        char = block[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return block[start : index + 1]
    return None


def _extract_value_expr(block: str, field_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(field_name)}\s*=", block)
    if match is None:
        return None
    index = match.end()
    while index < len(block) and block[index].isspace():
        index += 1

    start = index
    paren_depth = 0
    brace_depth = 0
    bracket_depth = 0
    in_string = ""
    escape = False

    while index < len(block):
        char = block[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = ""
        else:
            if char in ('"', "'"):
                in_string = char
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth = max(0, paren_depth - 1)
            elif char == "{":
                brace_depth += 1
            elif char == "}":
                if paren_depth == 0 and brace_depth == 0 and bracket_depth == 0:
                    break
                brace_depth = max(0, brace_depth - 1)
            elif char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif char in ",\n" and paren_depth == 0 and brace_depth == 0 and bracket_depth == 0:
                break
        index += 1
    return block[start:index].strip()


def _parse_damage_table(table_block: str | None) -> dict[str, float]:
    if not table_block:
        return {}
    damage_map: dict[str, float] = {}
    entry_re = re.compile(
        r"(?:\[\s*(?:engine\.)?DamageType\.([A-Z_]+)\s*\]|([A-Za-z_][A-Za-z0-9_]*))\s*=\s*([^,\n}]+)"
    )
    for match in entry_re.finditer(table_block):
        raw_key = match.group(1) or match.group(2) or ""
        key = _normalize_damage_type(raw_key)
        if not key:
            continue
        damage_map[key] = _parse_number_expr(match.group(3))
    return damage_map


def _parse_number_table(table_block: str | None) -> dict[str, float]:
    if not table_block:
        return {}
    values: dict[str, float] = {}
    entry_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,\n}]+)")
    for match in entry_re.finditer(table_block):
        values[match.group(1).lower()] = _parse_number_expr(match.group(2))
    return values


def _estimate_template_damage(
    weapon_dam: float,
    *,
    combat_dam: float,
    stats: dict[str, float],
    dammod: dict[str, float],
) -> float:
    if not stats and combat_dam <= 0.0:
        return weapon_dam
    return cm.estimate_combat_damage(
        weapon_dam,
        combat_dam=combat_dam,
        stats=stats,
        dammod=dammod or None,
    )


def _parse_damage_type_expr(expr: str | None, default: str = "PHYSICAL") -> str:
    if not expr:
        return default
    damage_type_match = re.search(r"(?:engine\.)?DamageType\.([A-Z_]+)", expr)
    if damage_type_match is not None:
        return _normalize_damage_type(damage_type_match.group(1)) or default
    string_match = re.search(r"""["']([^"']+)["']""", expr)
    if string_match is not None:
        return _normalize_damage_type(string_match.group(1)) or default
    return _normalize_damage_type(expr) or default


def _normalize_damage_type(raw_key: str) -> str:
    key = raw_key.strip().upper()
    if key in {
        "ALL",
        "PHYSICAL",
        "FIRE",
        "COLD",
        "LIGHTNING",
        "ACID",
        "NATURE",
        "ARCANE",
        "LIGHT",
        "DARKNESS",
        "BLIGHT",
        "TEMPORAL",
        "MIND",
        "STEAM",
    }:
        return key
    if key == "ICE":
        return "COLD"
    return ""


def _template_level(level_label: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", level_label)
    if match is None:
        return 0.0
    return float(match.group(0))


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _normalize_source_path(source_path: str) -> str:
    normalized = source_path.replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized
