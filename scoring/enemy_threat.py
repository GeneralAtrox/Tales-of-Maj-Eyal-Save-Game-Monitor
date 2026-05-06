"""Estimate incoming damage from an enemy as a fraction of player HP.

Port of `checkWeaponDanger` / `calcWeaponThreat` from the Danger Alert
addon (yutio888, 2019). Inputs are typed snapshots — the caller (e.g.
`gui/enemy_panel.py`) is responsible for extracting them from the
memory reader.

The key output is `weapon_threat_pct`: percent of player effective HP
(`max_life - die_at`) the enemy can remove per hit. Anything at or
above 100 means they can one-shot you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from . import combat_math as cm

# ── Inputs ──────────────────────────────────────────────────────────────────

_DAMAGE_TYPE_BY_ID: Final[dict[int, str]] = {
    1: "PHYSICAL",
    2: "ARCANE",
    3: "FIRE",
    4: "COLD",
    5: "LIGHTNING",
    6: "ACID",
    7: "NATURE",
    8: "BLIGHT",
    9: "LIGHT",
    10: "DARKNESS",
    11: "MIND",
    12: "TEMPORAL",
}


def _damage_type_from_field(value: str | float | bool | None) -> str:
    if isinstance(value, str):
        return cm.normalize_damage_type(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _DAMAGE_TYPE_BY_ID.get(int(value), cm.DEFAULT_DAMAGE_TYPE)
    return cm.DEFAULT_DAMAGE_TYPE


@dataclass(slots=True)
class PlayerDefenses:
    """Player-side inputs for threat estimation.

    All fields are optional (default 0) so a partial snapshot still
    produces a usable — if pessimistic — threat number.
    """

    max_life: float = 1.0
    die_at: float = 0.0
    armor: float = 0.0
    armor_hardiness_pct: float = 0.0
    defense: float = 0.0
    evasion_pct: float = 0.0
    resists: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    resists_cap: dict[str, float] = field(default_factory=dict)
    ignore_direct_crits_pct: float = 0.0
    """Chance (0..100) that a crit is ignored — the 'ignore_direct_crits' attr."""

    @property
    def effective_hp(self) -> float:
        return max(1.0, self.max_life - self.die_at)


@dataclass(slots=True)
class EnemyOffense:
    """Attacker-side inputs. Defaults mean 'no data' = zero threat."""

    name: str = ""
    rank: float = 1.0
    global_speed: float = 1.0
    atk: float = 0.0
    dam: float = 0.0
    apr: float = 0.0
    crit_chance_pct: float = 0.0
    crit_power_bonus_pct: float = 0.0
    """`combat.crit_power` — a percent bonus added on top of the 1.5 base."""
    physspeed: float = 1.0
    damage_type: str = cm.DEFAULT_DAMAGE_TYPE
    """Weapon damage type. ToME defaults melee weapons to PHYSICAL."""
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    talent_max_weapon_mult: float = 1.0
    """Largest weapon multiplier across the enemy's activated talents.

    1.0 = plain auto-attacks only. Supply a pre-computed value via the
    talent-db analysis; we don't compute it here to keep this module
    pure.
    """

    @classmethod
    def from_all_fields(cls, all_fields: dict[str, str | float | bool], name: str = "") -> EnemyOffense:
        """Build from `EntityInfo.all_fields` (the `_tab_dump_all` output)."""

        def num(key: str, default: float = 0.0) -> float:
            v = all_fields.get(key, default)
            if isinstance(v, (int, float)):
                return float(v)
            return default

        inc = {
            k.removeprefix("inc_damage."): float(v)
            for k, v in all_fields.items()
            if k.startswith("inc_damage.") and isinstance(v, (int, float))
        }
        pen = {
            k.removeprefix("resists_pen."): float(v)
            for k, v in all_fields.items()
            if k.startswith("resists_pen.") and isinstance(v, (int, float))
        }
        return cls(
            name=name or str(all_fields.get("name") or ""),
            rank=num("rank", 1.0),
            global_speed=num("global_speed", 1.0) or 1.0,
            atk=num("combat.atk"),
            dam=num("combat.dam"),
            apr=num("combat.apr"),
            crit_chance_pct=num("combat.crit"),
            crit_power_bonus_pct=num("combat.crit_power"),
            physspeed=num("combat.physspeed", 1.0) or 1.0,
            damage_type=_damage_type_from_field(all_fields.get("combat.damtype")),
            inc_damage=inc,
            resists_pen=pen,
        )


# ── Output ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ThreatReport:
    weapon_threat_pct: float
    """Final threat score: percent of effective HP per hit, after
    hit-rate and rank/speed scalars. ≥100 = can one-shot."""

    hit_rate_pct: float
    expected_damage: float
    raw_damage: float
    crit_chance_pct: float
    crit_used_pct: float
    """Crit chance actually used in the expected-damage calc (doubled
    for safety, clamped at 100)."""

    can_one_shot: bool
    damage_type: str
    worst_resist_type: str
    worst_resist_multiplier: float
    best_inc_type: str
    best_inc_pct: float
    notes: list[str]

    @property
    def tier_label(self) -> str:
        t = self.weapon_threat_pct
        if t >= 70:
            return "Deadly"
        if t >= 35:
            return "High"
        if t >= 20:
            return "Mediocre"
        return "Low"


# ── Core ────────────────────────────────────────────────────────────────────


RANK_BOSS_THRESHOLD: Final[float] = 3.0
RANK_BOSS_SCALAR: Final[float] = 1.2
"""Applied once when `rank > 3` (i.e. boss or above)."""

HIGH_THREAT_DOUBLE_HITRATE_PIVOT: Final[float] = 60.0
"""Below this, threat is scaled linearly by hit rate. Above it, hit
rate is doubled before scaling — high-damage enemies deserve worry
even if their nominal hit rate is modest."""


def weapon_threat(enemy: EnemyOffense, player: PlayerDefenses) -> ThreatReport:
    """Compute a single-hit weapon threat report.

    Mirrors `calcWeaponThreat` in the addon where useful, but uses the
    engine's type-specific damage/resist pipeline for the actual weapon
    damage type.
    """
    damage_type = cm.normalize_damage_type(enemy.damage_type)
    hit = cm.hit_rate(enemy.atk, player.defense, player.evasion_pct)
    after_armor = cm.armor_absorb(enemy.dam, player.armor, player.armor_hardiness_pct, enemy.apr)
    resist_mult = cm.resist_multiplier_for_type(
        player.resists,
        enemy.resists_pen,
        player.resists_cap,
        damage_type,
    )
    damage_inc = cm.damage_increase_for_type(enemy.inc_damage, damage_type)
    daminc_mult = 1.0 + damage_inc / 100.0

    crit_doubled = min(100.0, enemy.crit_chance_pct * 2.0)
    crit_power = enemy.crit_power_bonus_pct / 100.0 + cm.DEFAULT_CRIT_POWER
    if player.ignore_direct_crits_pct > 0:
        ignore = max(0.0, min(1.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    crit_mult = cm.crit_expected_multiplier(crit_doubled, crit_power)

    expected = after_armor * crit_mult * resist_mult * daminc_mult
    expected *= max(1.0, enemy.talent_max_weapon_mult)

    if enemy.rank > RANK_BOSS_THRESHOLD:
        expected *= RANK_BOSS_SCALAR
    if enemy.global_speed > 1.0:
        expected *= enemy.global_speed

    threat_pct = (expected / player.effective_hp) * 100.0
    if threat_pct < HIGH_THREAT_DOUBLE_HITRATE_PIVOT:
        threat_pct *= hit / 100.0
    else:
        threat_pct *= min(100.0, hit * 2.0) / 100.0

    notes: list[str] = []
    if expected >= player.effective_hp:
        notes.append(
            f"Can one-shot you ({expected:.0f} damage vs {player.effective_hp:.0f} effective HP)"
        )
    elif expected >= player.effective_hp * 0.7:
        notes.append(f"Can remove ~{expected / player.effective_hp * 100:.0f}% HP per hit")
    if hit >= 75:
        notes.append(f"Very likely to hit ({hit:.0f}%)")
    elif hit < 25:
        notes.append(f"Unlikely to hit ({hit:.0f}%)")
    if damage_inc >= 25:
        notes.append(f"Boosted {damage_type} damage: +{damage_inc:.0f}%")
    if enemy.global_speed > 1.0:
        notes.append(f"Acts {enemy.global_speed:.1f}x per turn")

    return ThreatReport(
        weapon_threat_pct=round(threat_pct, 1),
        hit_rate_pct=round(hit, 1),
        expected_damage=round(expected, 1),
        raw_damage=round(enemy.dam, 1),
        crit_chance_pct=enemy.crit_chance_pct,
        crit_used_pct=crit_doubled,
        can_one_shot=expected >= player.effective_hp,
        damage_type=damage_type,
        worst_resist_type=damage_type,
        worst_resist_multiplier=round(resist_mult, 3),
        best_inc_type=damage_type,
        best_inc_pct=damage_inc,
        notes=notes,
    )
