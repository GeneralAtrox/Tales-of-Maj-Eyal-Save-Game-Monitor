"""Estimate incoming damage from an enemy's *talents* (not auto-attacks).

Companion to `enemy_threat.py`. Reads each talent's parsed damage
metadata from `game_data.talent_db`, applies a simplified version of
the engine's scaling formula, then runs the result through the
player's resists / inc_damage multipliers — the same downstream
plumbing used for weapon hits.

This is intentionally conservative around targeting and cooldowns, but
the damage scaling itself follows ToME's `combatTalent*Damage` curve:
talent level and power stat are fed through the same square-root and
damage-rescale path used by the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from game_data.talent_db import TalentRecord, get_talent_db_by_id, resolve_target_range

from . import combat_math as cm
from .enemy_threat import PlayerDefenses

_RESOURCE_COST_FIELDS = (
    "mana",
    "stamina",
    "vim",
    "positive",
    "negative",
    "hate",
    "psi",
    "soul",
    "steam",
)


@dataclass(slots=True)
class EnemyPowers:
    """Power stats read from the enemy's combat table."""

    spellpower: float = 0.0
    mindpower: float = 0.0
    physicalpower: float = 0.0
    global_speed: float = 1.0
    weapon_range: float = 0.0
    atk: float = 0.0
    dam: float = 0.0
    apr: float = 0.0
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    talents: dict[str, int] = field(default_factory=dict)
    """``T_XXX`` → talent level (raw, before mastery)."""
    talents_cd: dict[str, int] = field(default_factory=dict)
    """``T_XXX`` → current cooldown turns visible on the actor."""
    resources: dict[str, float] = field(default_factory=dict)
    """Current actor resources read from memory, keyed by resource short name."""
    has_resource_snapshot: bool = False
    x: float | None = None
    y: float | None = None
    stats: dict[str, float] = field(default_factory=dict)
    """Base stat values used by ``combatTalentStatDamage`` talents."""
    spell_crit_pct: float = 0.0
    mind_crit_pct: float = 0.0
    physical_crit_pct: float = 0.0
    crit_power_bonus_pct: float = 0.0

    @property
    def has_talents(self) -> bool:
        return bool(self.talents)


def enemy_powers_from_fields(all_fields: dict[str, str | float | bool]) -> EnemyPowers:
    """Build talent-threat inputs from `EntityInfo.all_fields`."""

    stats = _number_fields_by_prefix(all_fields, "stats.")
    resources = _resource_fields(all_fields)
    return EnemyPowers(
        spellpower=_spell_power(all_fields, stats),
        mindpower=_mind_power(all_fields, stats),
        physicalpower=_physical_power(all_fields, stats),
        global_speed=_number_field(all_fields, "global_speed", 1.0) or 1.0,
        weapon_range=_number_field(all_fields, "combat.range"),
        atk=_number_field(all_fields, "combat.atk"),
        dam=_number_field(all_fields, "combat.dam"),
        apr=_number_field(all_fields, "combat_apr") + _number_field(all_fields, "combat.apr"),
        inc_damage=_number_fields_by_prefix(all_fields, "inc_damage."),
        resists_pen=_number_fields_by_prefix(all_fields, "resists_pen."),
        talents=_talent_fields_by_prefix(all_fields, "talents."),
        talents_cd=_cooldown_fields_by_prefix(all_fields, "talents_cd."),
        resources=resources,
        has_resource_snapshot=bool(resources),
        x=_optional_number_field(all_fields, "x"),
        y=_optional_number_field(all_fields, "y"),
        stats=stats,
        spell_crit_pct=_spell_crit(all_fields, stats),
        mind_crit_pct=_mind_crit(all_fields, stats),
        physical_crit_pct=_physical_crit(all_fields, stats),
        crit_power_bonus_pct=_number_field(all_fields, "combat_critical_power"),
    )


@dataclass(slots=True)
class TalentThreatEntry:
    talent_id: str
    talent_name: str
    damage_type: str
    raw_damage: float
    expected_damage: float
    threat_pct: float
    cooldown: int
    current_cooldown: int
    mode: str
    resource_shortages: dict[str, float] = field(default_factory=dict)
    range_to_target: float | None = None
    range_limit: float | None = None

    @property
    def is_available(self) -> bool:
        return self.current_cooldown <= 0 and not self.resource_shortages and not self.is_out_of_range

    @property
    def is_out_of_range(self) -> bool:
        if self.range_to_target is None or self.range_limit is None:
            return False
        return self.range_to_target > self.range_limit


@dataclass(slots=True)
class TalentThreatReport:
    max_expected_damage: float = 0.0
    max_threat_pct: float = 0.0
    max_available_expected_damage: float = 0.0
    max_available_threat_pct: float = 0.0
    worst_talent_id: str = ""
    worst_talent_name: str = ""
    worst_damage_type: str = ""
    worst_cooldown: int = 0
    worst_current_cooldown: int = 0
    worst_mode: str = ""
    entries: list[TalentThreatEntry] = field(default_factory=list)
    cc_tags: list[str] = field(default_factory=list)
    """Unique ``tactical.disable`` tags across all known talents — the
    enemy's crowd-control toolkit (stun, pin, disarm, ...)."""

    def strongest_available_entry(self) -> TalentThreatEntry | None:
        return next((entry for entry in self.entries if entry.is_available), None)


def talent_timing_label(
    mode: str,
    cooldown: int,
    current_cooldown: int = 0,
    resource_shortages: dict[str, float] | None = None,
    range_to_target: float | None = None,
    range_limit: float | None = None,
) -> str:
    """Compact display label for a parsed talent's activation context."""
    parts: list[str] = []
    normalized_mode = mode.strip().lower()
    if normalized_mode:
        parts.append(normalized_mode)
    if cooldown > 0:
        parts.append(f"cd {cooldown}")
    elif normalized_mode == "activated":
        parts.append("no cd")
    if current_cooldown > 0:
        parts.append(f"cooling {current_cooldown}")
    if resource_shortages:
        parts.extend(_resource_shortage_labels(resource_shortages))
    if range_to_target is not None and range_limit is not None and range_to_target > range_limit:
        parts.append(f"out of range {range_to_target:.0f}>{range_limit:.0f}")
    return ", ".join(parts)


def _scale_power_damage(record: TalentRecord, level: int, power: float) -> float:
    """Approximate `combatTalentScaledDamage` for non-weapon families."""
    if record.damage_high <= 0:
        return 0.0
    base = record.damage_low
    max_damage = record.damage_high
    talent_factor = (math.sqrt(max(1, level)) - 1.0) * 0.8 + 1.0
    max_factor = (math.sqrt(5.0) - 1.0) * 0.8 + 1.0
    mod = max_damage / ((base + 100.0) * max_factor)
    return cm.rescale_damage((base + max(0.0, power)) * talent_factor * mod)


def _scale_stat_damage(record: TalentRecord, level: int, stat_value: float) -> float:
    if record.damage_high <= 0:
        return 0.0
    base = record.damage_low
    max_damage = record.damage_high
    talent_factor = (math.sqrt(max(1, level)) - 1.0) * 0.8 + 1.0
    max_factor = (math.sqrt(5.0) - 1.0) * 0.8 + 1.0
    mod = max_damage / ((base + 100.0) * max_factor)
    raw = (base + max(0.0, stat_value)) * talent_factor * mod
    if raw <= 0.0 or record.scaling_no_dr:
        return raw
    return max(0.0, raw * (1.0 - math.log10(raw * 2.0) / 7.0))


def _scale(record: TalentRecord, level: int, powers: EnemyPowers) -> float:
    if record.scaling_family == "stat":
        return _scale_stat_damage(record, level, powers.stats.get(record.scaling_stat, 0.0))
    return _scale_power_damage(record, level, _power_for_family(record.scaling_family, powers))


def _power_for_family(family: str, powers: EnemyPowers) -> float:
    if family == "spell":
        return powers.spellpower
    if family == "mind":
        return powers.mindpower
    if family == "physical":
        return powers.physicalpower or powers.atk
    return 0.0


def _precomputed_or_raw(
    all_fields: dict[str, str | float | bool],
    precomputed_key: str,
    raw: float,
) -> float:
    if precomputed_key in all_fields:
        return max(0.0, _number_field(all_fields, precomputed_key))
    return max(0.0, raw)


def _spell_power(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    raw = _precomputed_or_raw(
        all_fields,
        "combat_precomputed_spellpower",
        _number_field(all_fields, "combat_spellpower")
        + _number_field(all_fields, "combat_generic_power")
        + stats.get("mag", 0.0),
    )
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _mind_power(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    raw = _precomputed_or_raw(
        all_fields,
        "combat_precomputed_mindpower",
        _number_field(all_fields, "combat_mindpower")
        + _number_field(all_fields, "combat_generic_power")
        + stats.get("wil", 0.0) * 0.7
        + stats.get("cun", 0.0) * 0.4,
    )
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _physical_power(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    raw = _precomputed_or_raw(
        all_fields,
        "combat_precomputed_physpower",
        _number_field(all_fields, "combat_dam")
        + _number_field(all_fields, "combat_generic_power")
        + stats.get("str", 0.0),
    )
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _crit_stat_bonus(stats: dict[str, float]) -> float:
    return (stats.get("cun", 10.0) - 10.0) * 0.3 + (stats.get("lck", 50.0) - 50.0) * 0.3


def _spell_crit(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    return min(
        100.0,
        max(
            0.0,
            _number_field(all_fields, "combat_spellcrit")
            + _number_field(all_fields, "combat_generic_crit")
            + _crit_stat_bonus(stats)
            + 1.0,
        ),
    )


def _mind_crit(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    return min(
        100.0,
        max(
            0.0,
            _number_field(all_fields, "combat_mindcrit")
            + _number_field(all_fields, "combat_generic_crit")
            + _crit_stat_bonus(stats)
            + 1.0,
        ),
    )


def _physical_crit(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    weapon_crit = _number_field(all_fields, "combat.physcrit", 1.0)
    return min(
        100.0,
        max(
            0.0,
            _number_field(all_fields, "combat_physcrit")
            + _number_field(all_fields, "combat_generic_crit")
            + _crit_stat_bonus(stats)
            + weapon_crit,
        ),
    )


def _crit_chance_for_record(record: TalentRecord, powers: EnemyPowers) -> float:
    if record.crit_family == "spell":
        return powers.spell_crit_pct
    if record.crit_family == "mind":
        return powers.mind_crit_pct
    if record.crit_family == "physical":
        return powers.physical_crit_pct
    return 0.0


def _crit_multiplier(record: TalentRecord, powers: EnemyPowers, player: PlayerDefenses) -> float:
    crit_chance = _crit_chance_for_record(record, powers)
    if crit_chance <= 0.0:
        return 1.0
    crit_power = cm.DEFAULT_CRIT_POWER + powers.crit_power_bonus_pct / 100.0
    if player.ignore_direct_crits_pct > 0.0:
        ignore = min(1.0, max(0.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    return cm.crit_expected_multiplier(crit_chance, crit_power)


def compute_talent_threat(
    powers: EnemyPowers,
    player: PlayerDefenses,
    *,
    db: dict[str, TalentRecord] | None = None,
) -> TalentThreatReport:
    """Walk the enemy's talents and return the scariest-expected one."""
    report = TalentThreatReport()
    if not powers.talents:
        return report

    records = db if db is not None else get_talent_db_by_id()
    cc: set[str] = set()

    for tid, level in powers.talents.items():
        record = records.get(tid)
        if record is None:
            continue
        if not record.npc_usable:
            continue
        if record.mode == "passive":
            continue
        if record.tactical_disable:
            cc.update(record.tactical_disable)
        if not record.scaling_family or record.scaling_family == "weapon":
            # Weapon-family talents feed into `talent_max_weapon_mult`
            # on `EnemyOffense`; don't double-count here.
            continue
        if record.damage_high <= 0:
            continue

        raw = _scale(record, int(level), powers)
        if raw <= 0:
            continue

        # Apply the same downstream multipliers as weapon_threat, but
        # skip armor (talents typically bypass it) unless this is a
        # physical-scaling talent that does PHYSICAL damage.
        if not record.damage_type:
            continue
        dtype = cm.normalize_damage_type(record.damage_type, "all")
        mult = cm.resist_multiplier_for_type(
            player.resists,
            powers.resists_pen,
            player.resists_cap,
            dtype or "all",
        )
        after = raw
        if record.scaling_family == "physical" and dtype == "PHYSICAL":
            after = cm.armor_absorb(
                raw, player.armor, player.armor_hardiness_pct, powers.apr
            )
        daminc_mult = 1.0 + cm.damage_increase_for_type(powers.inc_damage, dtype or "all") / 100.0
        expected = after * mult * daminc_mult * _crit_multiplier(record, powers, player)
        threat_pct = (expected / player.effective_hp) * 100.0 * _speed_threat_scalar(powers)
        range_to_target, range_limit = _range_check(record, powers, player, records)

        entry = TalentThreatEntry(
            talent_id=tid,
            talent_name=_name_for(tid, records),
            damage_type=dtype,
            raw_damage=round(raw, 1),
            expected_damage=round(expected, 1),
            threat_pct=round(threat_pct, 1),
            cooldown=record.cooldown,
            current_cooldown=powers.talents_cd.get(tid, 0),
            mode=record.mode,
            resource_shortages=_resource_shortages(record.resource_costs, powers),
            range_to_target=range_to_target,
            range_limit=range_limit,
        )
        report.entries.append(entry)
        if expected > report.max_expected_damage:
            report.max_expected_damage = expected
            report.max_threat_pct = threat_pct
            report.worst_talent_id = tid
            report.worst_talent_name = entry.talent_name
            report.worst_damage_type = dtype
            report.worst_cooldown = entry.cooldown
            report.worst_current_cooldown = entry.current_cooldown
            report.worst_mode = entry.mode
        if entry.is_available and expected > report.max_available_expected_damage:
            report.max_available_expected_damage = expected
            report.max_available_threat_pct = threat_pct

    report.entries.sort(key=lambda e: e.expected_damage, reverse=True)
    report.cc_tags = sorted(cc)
    report.max_expected_damage = round(report.max_expected_damage, 1)
    report.max_threat_pct = round(report.max_threat_pct, 1)
    report.max_available_expected_damage = round(report.max_available_expected_damage, 1)
    report.max_available_threat_pct = round(report.max_available_threat_pct, 1)
    return report


def _speed_threat_scalar(powers: EnemyPowers) -> float:
    return max(1.0, powers.global_speed or 1.0)


def _name_for(tid: str, records: dict[str, TalentRecord]) -> str:
    rec = records.get(tid)
    if rec is None:
        return tid
    # Prefer a human name: strip T_ prefix, title-case underscores.
    if tid.startswith("T_"):
        return tid[2:].replace("_", " ").title()
    return tid


def _number_field(all_fields: dict[str, str | float | bool], key: str, default: float = 0.0) -> float:
    value = all_fields.get(key, default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _optional_number_field(all_fields: dict[str, str | float | bool], key: str) -> float | None:
    value = all_fields.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _number_fields_by_prefix(all_fields: dict[str, str | float | bool], prefix: str) -> dict[str, float]:
    return {
        key.removeprefix(prefix): float(value)
        for key, value in all_fields.items()
        if key.startswith(prefix) and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _resource_fields(all_fields: dict[str, str | float | bool]) -> dict[str, float]:
    resources: dict[str, float] = {}
    for key in _RESOURCE_COST_FIELDS:
        value = all_fields.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            resources[key] = float(value)
    return resources


def _talent_fields_by_prefix(all_fields: dict[str, str | float | bool], prefix: str) -> dict[str, int]:
    talents: dict[str, int] = {}
    for key, value in all_fields.items():
        if not key.startswith(prefix) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        level = int(value)
        if level <= 0:
            continue
        talent_id = key.removeprefix(prefix).strip().upper()
        if not talent_id:
            continue
        if not talent_id.startswith("T_"):
            talent_id = f"T_{talent_id}"
        talents[talent_id] = level
    return talents


def _cooldown_fields_by_prefix(all_fields: dict[str, str | float | bool], prefix: str) -> dict[str, int]:
    cooldowns: dict[str, int] = {}
    for key, value in all_fields.items():
        if not key.startswith(prefix) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        cooldown = math.ceil(float(value))
        if cooldown <= 0:
            continue
        talent_id = key.removeprefix(prefix).strip().upper()
        if not talent_id:
            continue
        if not talent_id.startswith("T_"):
            talent_id = f"T_{talent_id}"
        cooldowns[talent_id] = cooldown
    return cooldowns


def _resource_shortages(costs: dict[str, float], powers: EnemyPowers) -> dict[str, float]:
    if not costs or not powers.has_resource_snapshot:
        return {}
    shortages: dict[str, float] = {}
    for resource, cost in costs.items():
        current = powers.resources.get(resource)
        if current is None:
            shortages[resource] = cost
        elif current < cost:
            shortages[resource] = cost - current
    return shortages


def _resource_shortage_labels(shortages: dict[str, float]) -> list[str]:
    labels: list[str] = []
    for resource, missing in sorted(shortages.items()):
        if missing <= 0:
            continue
        missing_text = str(int(missing)) if float(missing).is_integer() else f"{missing:.1f}"
        labels.append(f"needs {resource} +{missing_text}")
    return labels


def _range_check(
    record: TalentRecord,
    powers: EnemyPowers,
    player: PlayerDefenses,
    records: dict[str, TalentRecord],
) -> tuple[float | None, float | None]:
    if not record.requires_target:
        return None, None
    target_range = resolve_target_range(record, records, weapon_range=powers.weapon_range)
    if target_range is None:
        return None, None
    if powers.x is None or powers.y is None or player.x is None or player.y is None:
        return None, None
    distance = max(abs(powers.x - player.x), abs(powers.y - player.y))
    return distance, max(0.0, target_range + max(0.0, record.target_radius))
