"""Estimate incoming damage from an enemy's *talents* (not auto-attacks).

Companion to `enemy_threat.py`. Reads each talent's parsed damage
metadata from `game_data.talent_db`, applies a simplified version of
the engine's scaling formula, then runs the result through the
player's resists / inc_damage multipliers — the same downstream
plumbing used for weapon hits.

This is intentionally conservative: ToME's real formula combines
talent level, power stat, and mastery in a non-trivial way. We
approximate with ``base + (max - base) * min(1, level * power / 500)``
which gives a reasonable midpoint without needing to reproduce every
engine branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from game_data.talent_db import TalentRecord, get_talent_db_by_id

from . import combat_math as cm
from .enemy_threat import PlayerDefenses

_POWER_LEVEL_DIVISOR: Final[float] = 500.0
"""``level * power`` at which a talent is assumed to reach its ``max``
value. Chosen so L5 + 100 power → full scaling; L3 + 50 power → ~30%."""


@dataclass(slots=True)
class EnemyPowers:
    """Power stats read from the enemy's combat table."""

    spellpower: float = 0.0
    mindpower: float = 0.0
    physicalpower: float = 0.0
    atk: float = 0.0
    dam: float = 0.0
    apr: float = 0.0
    resists_pen: dict[str, float] = field(default_factory=dict)
    talents: dict[str, int] = field(default_factory=dict)
    """``T_XXX`` → talent level (raw, before mastery)."""


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
    low = record.damage_low
    span = record.damage_high - low
    if span <= 0:
        return low
    progress = (max(1, level) * max(0.0, power)) / _POWER_LEVEL_DIVISOR
    progress = min(1.0, progress)
    return low + span * progress


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
        dtype = record.damage_type or ""
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
        expected = after * mult
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
