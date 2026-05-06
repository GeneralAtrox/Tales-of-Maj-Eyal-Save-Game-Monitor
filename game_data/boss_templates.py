from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from game_data.lua_extractor import RE_NAME, find_tome_team, iter_balanced_blocks
from scoring import combat_math as cm
from scoring.ranks import rank_label
from scoring.talent_weapon import weapon_multipliers_for_talents

_BASE_TEMPLATE_RE = re.compile(r'define_as\s*=\s*"BASE_')
_BASE_RE = re.compile(r'\bbase\s*=\s*"([^"]+)"')
_DEFINE_AS_RE = re.compile(r'\bdefine_as\s*=\s*"([^"]+)"')
_TYPE_RE = re.compile(r'^[^\n{]*\btype\s*=\s*"([^"]+)"', re.MULTILINE)
_SUBTYPE_RE = re.compile(r'^[^\n{]*\bsubtype\s*=\s*"([^"]+)"', re.MULTILINE)
_FACTION_RE = re.compile(r'^[^\n{]*\bfaction\s*=\s*"([^"]+)"', re.MULTILINE)
_AUTOLEVEL_RE = re.compile(r'^[^\n{]*\bautolevel\s*=\s*"([^"]+)"', re.MULTILINE)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_TALENT_ENTRY_RE = re.compile(
    r"\[\s*(?:(?:ActorTalents|Talents)\.)?(T_[A-Z0-9_]+)\s*\]\s*=\s*(\{[^}]*\}|[^,\n}]+)"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDORED_DATA_ROOT = _REPO_ROOT / "tools" / "t-engine4-master" / "game" / "modules" / "tome" / "data"
_AUTOLEVEL_STAT_GAINS: dict[str, dict[str, float]] = {
    "warrior": {"str": 2, "dex": 1},
    "ghoul": {"str": 1, "con": 1},
    "toad": {"dex": 1, "con": 1},
    "zerker": {"str": 2, "con": 1},
    "tank": {"str": 1, "con": 2},
    "rogue": {"dex": 1, "cun": 2},
    "slinger": {"dex": 2, "cun": 1},
    "archer": {"dex": 2, "str": 1},
    "caster": {"mag": 2, "wil": 1},
    "wisecaster": {"mag": 1, "wil": 1},
    "warriormage": {"mag": 2, "wil": 1, "str": 2, "dex": 1},
    "roguemage": {"cun": 2, "dex": 1, "mag": 1},
    "dexmage": {"mag": 2, "dex": 2},
    "snake": {"cun": 2, "dex": 2, "con": 1, "str": 1},
    "spider": {"cun": 1, "wil": 1, "mag": 1, "dex": 2},
    "alchemy-golem": {"str": 2, "dex": 1, "con": 1},
    "drake": {"str": 2, "wil": 2, "con": 1, "dex": 1},
    "wildcaster": {"wil": 2, "cun": 1},
    "summoner": {"wil": 1, "cun": 1},
    "wyrmic": {"str": 1, "wil": 1, "dex": 1, "cun": 1},
    "warriorwill": {"str": 2, "wil": 2, "dex": 1},
    "butcher": {"str": 1, "con": 1, "cun": 1},
}


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
    talent_max_weapon_mult: float
    talent_burst_weapon_mult: float
    talent_burst_weapon_hits: int
    spellpower: float
    mindpower: float
    physicalpower: float
    spell_crit_pct: float
    mind_crit_pct: float
    physical_crit_pct: float
    stats: dict[str, float] = field(default_factory=dict)
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    talents: dict[str, int] = field(default_factory=dict)
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
            talent_max_weapon_mult=1.0,
            talent_burst_weapon_mult=1.0,
            talent_burst_weapon_hits=1,
            spellpower=0.0,
            mindpower=0.0,
            physicalpower=0.0,
            spell_crit_pct=0.0,
            mind_crit_pct=0.0,
            physical_crit_pct=0.0,
        )

    block = boss_block.block
    base_block = _resolve_base_block(block)
    source_blocks = (block, base_block.block) if base_block is not None else (block,)
    combat_block = _extract_table_from_blocks(source_blocks, "combat")
    inc_damage = _parse_merged_damage_table(source_blocks, "inc_damage")
    resists_pen = _parse_merged_damage_table(source_blocks, "resists_pen")
    talents = _parse_merged_talent_table(source_blocks)
    weapon_dam = _combat_value(combat_block, "dam")
    level = _parse_scalar_from_blocks(source_blocks, "level_range", default=_template_level(template.level_label))
    stats = _autoleveled_stats(
        _parse_merged_number_table(source_blocks, "stats"),
        _parse_string(source_blocks, _AUTOLEVEL_RE),
        level,
    )
    dammod = _parse_number_table(_extract_table(combat_block or "", "dammod"))
    combat_dam = _parse_scalar_from_blocks(source_blocks, "combat_dam")
    dam = _estimate_template_damage(
        weapon_dam,
        combat_dam=combat_dam,
        stats=stats,
        dammod=dammod,
    )
    atk = _combat_value(combat_block, "atk")
    apr = _combat_value(combat_block, "apr")
    crit = (
        _parse_scalar_from_blocks(source_blocks, "combat_physcrit")
        + _parse_scalar_from_blocks(source_blocks, "combat_generic_crit")
        + _combat_value(combat_block, "physcrit")
    )
    if crit == 0.0:
        crit = _combat_value(combat_block, "crit")
    crit_power = _parse_scalar_from_blocks(source_blocks, "combat_critical_power") + _combat_value(
        combat_block, "crit_power"
    )
    physspeed = _combat_value(combat_block, "physspeed")
    if physspeed == 0.0:
        physspeed = _parse_scalar_from_blocks(source_blocks, "combat_physspeed", default=1.0)
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

    rank = _parse_scalar_from_blocks(source_blocks, "rank", default=4.0)
    weapon_mults = weapon_multipliers_for_talents(talents)
    return BossTemplateStats(
        template=template,
        name=template.name,
        level=level,
        max_life=_parse_scalar_from_blocks(source_blocks, "max_life"),
        rank=rank,
        rank_name=rank_label(rank),
        faction=_parse_string(source_blocks, _FACTION_RE),
        type_name=_parse_string(source_blocks, _TYPE_RE),
        subtype=_parse_string(source_blocks, _SUBTYPE_RE),
        global_speed=_parse_scalar_from_blocks(
            source_blocks,
            "global_speed_base",
            default=_parse_scalar_from_blocks(source_blocks, "global_speed", default=1.0),
        )
        or 1.0,
        dam=dam,
        atk=atk,
        apr=apr,
        crit_chance_pct=crit,
        crit_power_bonus_pct=crit_power,
        physspeed=physspeed or 1.0,
        damage_type=damage_type,
        talent_max_weapon_mult=weapon_mults.max_hit,
        talent_burst_weapon_mult=weapon_mults.burst,
        talent_burst_weapon_hits=weapon_mults.burst_hits,
        spellpower=_template_spell_power(
            _parse_scalar_from_blocks(source_blocks, "combat_spellpower"),
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_power"),
        ),
        mindpower=_template_mind_power(
            _parse_scalar_from_blocks(source_blocks, "combat_mindpower"),
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_power"),
        ),
        physicalpower=_template_physical_power(
            combat_dam,
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_power"),
        ),
        spell_crit_pct=_template_spell_crit(
            _parse_scalar_from_blocks(source_blocks, "combat_spellcrit"),
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_crit"),
        ),
        mind_crit_pct=_template_mind_crit(
            _parse_scalar_from_blocks(source_blocks, "combat_mindcrit"),
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_crit"),
        ),
        physical_crit_pct=_template_physical_crit(
            _parse_scalar_from_blocks(source_blocks, "combat_physcrit"),
            stats,
            _parse_scalar_from_blocks(source_blocks, "combat_generic_crit"),
            _combat_value(combat_block, "physcrit"),
        ),
        stats=dict(stats),
        inc_damage=inc_damage,
        resists_pen=resists_pen,
        talents={talent_id: int(level) for talent_id, level in talents.items() if level > 0.0},
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


@lru_cache(maxsize=1)
def _define_block_map() -> dict[str, _BossBlock]:
    define_map: dict[str, _BossBlock] = {}
    for source_path, lua in _iter_npc_sources():
        for block in iter_balanced_blocks(lua, "newEntity"):
            define_as_match = _DEFINE_AS_RE.search(block)
            if define_as_match is None:
                continue
            define_map[define_as_match.group(1).strip()] = _BossBlock(source_path=source_path, block=block)
    return define_map


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


def _resolve_base_block(block: str) -> _BossBlock | None:
    base_match = _BASE_RE.search(block)
    if base_match is None:
        return None
    return _define_block_map().get(base_match.group(1).strip())


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


def _as_blocks(blocks: str | tuple[str, ...]) -> tuple[str, ...]:
    return (blocks,) if isinstance(blocks, str) else blocks


def _parse_string(blocks: str | tuple[str, ...], pattern: re.Pattern[str]) -> str:
    for block in _as_blocks(blocks):
        match = pattern.search(block)
        if match is not None:
            return match.group(1).strip()
    return ""


def _parse_scalar_from_blocks(blocks: tuple[str, ...], field_name: str, default: float = 0.0) -> float:
    for block in blocks:
        expr = _extract_value_expr(block, field_name)
        if expr is not None:
            return _parse_number_expr(expr, default=default)
    return default


def _extract_table_from_blocks(blocks: tuple[str, ...], field_name: str) -> str | None:
    for block in blocks:
        table_block = _extract_table(block, field_name)
        if table_block is not None:
            return table_block
    return None


def _parse_merged_damage_table(blocks: tuple[str, ...], field_name: str) -> dict[str, float]:
    merged: dict[str, float] = {}
    for block in reversed(blocks):
        merged.update(_parse_damage_table(_extract_table(block, field_name)))
    return merged


def _parse_merged_number_table(blocks: tuple[str, ...], field_name: str) -> dict[str, float]:
    merged: dict[str, float] = {}
    for block in reversed(blocks):
        merged.update(_parse_number_table(_extract_table(block, field_name)))
    return merged


def _parse_merged_talent_table(blocks: tuple[str, ...]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for block in reversed(blocks):
        merged.update(_parse_talent_table(block))
    return merged


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


def _parse_talent_table(block: str) -> dict[str, float]:
    talents: dict[str, float] = {}
    for match in _TALENT_ENTRY_RE.finditer(block):
        talent_id = match.group(1).strip().upper()
        talents[talent_id] = _parse_talent_level(match.group(2))
    return talents


def _parse_talent_level(expr: str) -> float:
    cleaned = expr.strip()
    if cleaned.startswith("{"):
        max_level = _parse_scalar_field(cleaned, "max")
        if max_level > 0.0:
            return max_level
        base_level = _parse_scalar_field(cleaned, "base")
        if base_level > 0.0:
            return base_level
    return _parse_number_expr(cleaned)


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


def _autoleveled_stats(stats: dict[str, float], autolevel: str, level: float) -> dict[str, float]:
    gains = _AUTOLEVEL_STAT_GAINS.get(autolevel)
    if not gains or level <= 1.0:
        return stats
    leveled = dict(stats)
    levelups = max(0.0, level - 1.0)
    for stat, gain in gains.items():
        leveled[stat] = leveled.get(stat, 0.0) + gain * levelups
    return leveled


def _template_spell_power(combat_spellpower: float, stats: dict[str, float], generic_power: float = 0.0) -> float:
    raw = max(0.0, combat_spellpower + generic_power + stats.get("mag", 0.0))
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _template_mind_power(combat_mindpower: float, stats: dict[str, float], generic_power: float = 0.0) -> float:
    raw = max(
        0.0,
        combat_mindpower + generic_power + stats.get("wil", 0.0) * 0.7 + stats.get("cun", 0.0) * 0.4,
    )
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _template_physical_power(combat_dam: float, stats: dict[str, float], generic_power: float = 0.0) -> float:
    raw = max(0.0, combat_dam + generic_power + stats.get("str", 0.0))
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _template_crit_stat_bonus(stats: dict[str, float]) -> float:
    return (stats.get("cun", 10.0) - 10.0) * 0.3 + (stats.get("lck", 50.0) - 50.0) * 0.3


def _template_spell_crit(combat_spellcrit: float, stats: dict[str, float], generic_crit: float = 0.0) -> float:
    return max(0.0, min(100.0, combat_spellcrit + generic_crit + _template_crit_stat_bonus(stats) + 1.0))


def _template_mind_crit(combat_mindcrit: float, stats: dict[str, float], generic_crit: float = 0.0) -> float:
    return max(0.0, min(100.0, combat_mindcrit + generic_crit + _template_crit_stat_bonus(stats) + 1.0))


def _template_physical_crit(
    combat_physcrit: float,
    stats: dict[str, float],
    generic_crit: float = 0.0,
    weapon_crit: float = 0.0,
) -> float:
    return max(0.0, min(100.0, combat_physcrit + generic_crit + _template_crit_stat_bonus(stats) + weapon_crit))


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
