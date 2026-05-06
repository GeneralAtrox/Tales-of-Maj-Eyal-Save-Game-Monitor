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

from game_data.talent_db import TalentRecord, get_talent_db_by_id

from . import combat_math as cm
from .enemy_threat import PlayerDefenses

@dataclass(slots=True)
class EnemyPowers:
    """Power stats read from the enemy's combat table."""

    spellpower: float = 0.0
    mindpower: float = 0.0
    physicalpower: float = 0.0
    atk: float = 0.0
    dam: float = 0.0
    apr: float = 0.0
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    talents: dict[str, int] = field(default_factory=dict)
    """``T_XXX`` → talent level (raw, before mastery)."""

    @property
    def has_talents(self) -> bool:
        return bool(self.talents)


def enemy_powers_from_fields(all_fields: dict[str, str | float | bool]) -> EnemyPowers:
    """Build talent-threat inputs from `EntityInfo.all_fields`."""

    stats = _number_fields_by_prefix(all_fields, "stats.")
    physical_raw = _number_field(all_fields, "combat_dam") + stats.get("str", 0.0)
    physicalpower = cm.rescale_combat_stats(max(0.0, physical_raw)) if physical_raw > 0.0 else 0.0
    return EnemyPowers(
        spellpower=_number_field(all_fields, "combat_spellpower"),
        mindpower=_number_field(all_fields, "combat_mindpower"),
        physicalpower=physicalpower,
        atk=_number_field(all_fields, "combat.atk"),
        dam=_number_field(all_fields, "combat.dam"),
        apr=_number_field(all_fields, "combat.apr"),
        inc_damage=_number_fields_by_prefix(all_fields, "inc_damage."),
        resists_pen=_number_fields_by_prefix(all_fields, "resists_pen."),
        talents=_talent_fields_by_prefix(all_fields, "talents."),
    )


@dataclass(slots=True)
class TalentThreatEntry:
    talent_id: str
    talent_name: str
    damage_type: str
    raw_damage: float
    expected_damage: float
    threat_pct: float


@dataclass(slots=True)
class TalentThreatReport:
    max_expected_damage: float = 0.0
    max_threat_pct: float = 0.0
    worst_talent_id: str = ""
    worst_talent_name: str = ""
    worst_damage_type: str = ""
    entries: list[TalentThreatEntry] = field(default_factory=list)
    cc_tags: list[str] = field(default_factory=list)
    """Unique ``tactical.disable`` tags across all known talents — the
    enemy's crowd-control toolkit (stun, pin, disarm, ...)."""


def _scale(record: TalentRecord, level: int, power: float) -> float:
    """Approximate `combatTalentScaledDamage` for non-weapon families."""
    if record.damage_high <= 0:
        return 0.0
    base = record.damage_low
    max_damage = record.damage_high
    talent_factor = (math.sqrt(max(1, level)) - 1.0) * 0.8 + 1.0
    max_factor = (math.sqrt(5.0) - 1.0) * 0.8 + 1.0
    mod = max_damage / ((base + 100.0) * max_factor)
    return cm.rescale_damage((base + max(0.0, power)) * talent_factor * mod)


def _power_for_family(family: str, powers: EnemyPowers) -> float:
    if family == "spell":
        return powers.spellpower
    if family == "mind":
        return powers.mindpower
    if family == "physical":
        return powers.physicalpower or powers.atk
    return 0.0


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
        if record.tactical_disable:
            cc.update(record.tactical_disable)
        if not record.scaling_family or record.scaling_family == "weapon":
            # Weapon-family talents feed into `talent_max_weapon_mult`
            # on `EnemyOffense`; don't double-count here.
            continue
        if record.damage_high <= 0:
            continue

        power = _power_for_family(record.scaling_family, powers)
        raw = _scale(record, int(level), power)
        if raw <= 0:
            continue

        # Apply the same downstream multipliers as weapon_threat, but
        # skip armor (talents typically bypass it) unless this is a
        # physical-scaling talent that does PHYSICAL damage.
        dtype = cm.normalize_damage_type(record.damage_type, "all") if record.damage_type else "all"
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
        expected = after * mult * daminc_mult
        threat_pct = (expected / player.effective_hp) * 100.0

        entry = TalentThreatEntry(
            talent_id=tid,
            talent_name=_name_for(tid, records),
            damage_type=dtype,
            raw_damage=round(raw, 1),
            expected_damage=round(expected, 1),
            threat_pct=round(threat_pct, 1),
        )
        report.entries.append(entry)
        if expected > report.max_expected_damage:
            report.max_expected_damage = expected
            report.max_threat_pct = threat_pct
            report.worst_talent_id = tid
            report.worst_talent_name = entry.talent_name
            report.worst_damage_type = dtype

    report.entries.sort(key=lambda e: e.expected_damage, reverse=True)
    report.cc_tags = sorted(cc)
    report.max_expected_damage = round(report.max_expected_damage, 1)
    report.max_threat_pct = round(report.max_threat_pct, 1)
    return report


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


def _number_fields_by_prefix(all_fields: dict[str, str | float | bool], prefix: str) -> dict[str, float]:
    return {
        key.removeprefix(prefix): float(value)
        for key, value in all_fields.items()
        if key.startswith(prefix) and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


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
