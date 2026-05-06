"""Estimate incoming damage from an enemy as a fraction of player HP.

Port of `checkWeaponDanger` / `calcWeaponThreat` from the Danger Alert
addon (yutio888, 2019). Inputs are typed snapshots — the caller (e.g.
`gui/enemy_panel.py`) is responsible for extracting them from the
memory reader.

The key output is `weapon_threat_pct`: percent of player effective HP
(`max_life - die_at`) represented by the enemy's current weapon danger.
It starts from single-hit damage, then applies hit-rate and pacing risk
scalars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from . import combat_math as cm
from .talent_weapon import WeaponTalentMultipliers, weapon_multipliers_for_talents

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
_RESOURCE_COST_FIELDS: Final[tuple[str, ...]] = (
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
    combat_crit_reduction_pct: float = 0.0
    """Flat physical weapon crit chance reduction from ``combat_crit_reduction``."""
    x: float | None = None
    y: float | None = None

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
    """`combat_critical_power` plus weapon crit power, as a percent bonus above the 1.5 base."""
    accuracy_effect: str = ""
    """Weapon accuracy effect kind (`accuracy_effect` or `talented` in ToME combat tables)."""
    accuracy_effect_scale: bool = False
    """ToME treats a truthy `accuracy_effect_scale` as a half-strength accuracy effect."""
    damage_range: float = 1.0
    """Weapon damage roll range. ToME rolls from base damage to base damage * this value before armor."""
    physspeed: float = 1.0
    weapon_range: float = 0.0
    damage_type: str = cm.DEFAULT_DAMAGE_TYPE
    """Weapon damage type. ToME defaults melee weapons to PHYSICAL."""
    inc_damage: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    melee_project: dict[str, float] = field(default_factory=dict)
    """Extra damage projected on every successful melee hit."""
    burst_on_hit: dict[str, float] = field(default_factory=dict)
    """Extra radius-1 project damage on every successful melee hit."""
    burst_on_crit: dict[str, float] = field(default_factory=dict)
    """Extra radius-2 project damage on successful melee crits."""
    talents: dict[str, float] = field(default_factory=dict)
    talents_cd: dict[str, float] = field(default_factory=dict)
    resources: dict[str, float] = field(default_factory=dict)
    has_resource_snapshot: bool = False
    x: float | None = None
    y: float | None = None
    talent_max_weapon_mult: float = 1.0
    """Largest weapon multiplier across the enemy's activated talents.

    1.0 = plain auto-attacks only. Supply a pre-computed value via the
    talent-db analysis; we don't compute it here to keep this module
    pure.
    """
    talent_burst_weapon_mult: float = 1.0
    """Largest summed same-action weapon multiplier across visible talents."""
    talent_burst_weapon_hits: int = 1
    """Number of direct weapon hits in the strongest same-action burst."""

    def weapon_multipliers_against(self, player: PlayerDefenses) -> WeaponTalentMultipliers:
        if not self.talents:
            return WeaponTalentMultipliers(
                max_hit=self.talent_max_weapon_mult,
                burst=self.talent_burst_weapon_mult,
                burst_hits=self.talent_burst_weapon_hits,
            )
        return weapon_multipliers_for_talents(
            self.talents,
            cooldowns=self.talents_cd,
            resources=self.resources if self.has_resource_snapshot else None,
            range_to_target=_range_to_target(self, player),
            weapon_range=self.weapon_range,
        )

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
        resources = _resource_fields(all_fields)
        stats = _number_fields_by_prefix(all_fields, "stats.")
        talents = _number_fields_by_prefix(all_fields, "talents.")
        talents_cd = _number_fields_by_prefix(all_fields, "talents_cd.")
        weapon_mults = weapon_multipliers_for_talents(
            talents,
            cooldowns=talents_cd,
            resources=resources or None,
        )
        return cls(
            name=name or str(all_fields.get("name") or ""),
            rank=num("rank", 1.0),
            global_speed=num("global_speed", 1.0) or 1.0,
            atk=_combat_attack(all_fields, stats, num("combat.atk")),
            dam=_melee_damage(all_fields, num("combat.dam")),
            apr=_combat_apr(all_fields, num("combat.apr")),
            crit_chance_pct=_physical_crit_chance(all_fields, stats),
            crit_power_bonus_pct=_physical_crit_power_bonus(all_fields),
            accuracy_effect=_accuracy_effect_from_fields(all_fields),
            accuracy_effect_scale=_truthy_field(all_fields, "combat.accuracy_effect_scale"),
            damage_range=_combat_damage_range(all_fields),
            physspeed=_combat_physical_speed(all_fields, num("combat.physspeed", 1.0)),
            weapon_range=num("combat.range"),
            damage_type=_damage_type_from_field(
                all_fields.get("force_melee_damtype") or all_fields.get("combat.damtype")
            ),
            inc_damage=inc,
            resists_pen=pen,
            melee_project=_damage_fields_by_prefixes(all_fields, "combat.melee_project.", "melee_project."),
            burst_on_hit=_damage_fields_by_prefixes(all_fields, "combat.burst_on_hit."),
            burst_on_crit=_damage_fields_by_prefixes(all_fields, "combat.burst_on_crit."),
            talents=talents,
            talents_cd=talents_cd,
            resources=resources,
            has_resource_snapshot=bool(resources),
            x=_optional_number_field(all_fields, "x"),
            y=_optional_number_field(all_fields, "y"),
            talent_max_weapon_mult=weapon_mults.max_hit,
            talent_burst_weapon_mult=weapon_mults.burst,
            talent_burst_weapon_hits=weapon_mults.burst_hits,
        )


# ── Output ──────────────────────────────────────────────────────────────────


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


def _range_to_target(enemy: EnemyOffense, player: PlayerDefenses) -> float | None:
    if enemy.x is None or enemy.y is None or player.x is None or player.y is None:
        return None
    return max(abs(enemy.x - player.x), abs(enemy.y - player.y))


def _combat_attack(
    all_fields: dict[str, str | float | bool],
    stats: dict[str, float],
    weapon_atk: float,
) -> float:
    if "combat_precomputed_accuracy" in all_fields:
        return max(0.0, _number_field(all_fields, "combat_precomputed_accuracy"))
    raw = max(
        0.0,
        4.0
        + _number_field(all_fields, "combat_atk")
        + weapon_atk
        + (stats.get("lck", 50.0) - 50.0) * 0.4
        + (stats.get("dex", 10.0) - 10.0),
    )
    return cm.rescale_combat_stats(raw) if raw > 0.0 else 0.0


def _combat_apr(all_fields: dict[str, str | float | bool], weapon_apr: float) -> float:
    return _number_field(all_fields, "combat_apr") + weapon_apr


def _combat_damage_range(all_fields: dict[str, str | float | bool]) -> float:
    weapon_range = _number_field(all_fields, "combat.damrange", 1.1)
    return (_number_field(all_fields, "combat_damrange") + weapon_range) or 1.0


def _combat_physical_speed(all_fields: dict[str, str | float | bool], weapon_physspeed: float) -> float:
    actor_physspeed = max(_number_field(all_fields, "combat_physspeed", 1.0), 0.4)
    return (weapon_physspeed or 1.0) / actor_physspeed


def _crit_stat_bonus(stats: dict[str, float]) -> float:
    return (stats.get("cun", 10.0) - 10.0) * 0.3 + (stats.get("lck", 50.0) - 50.0) * 0.3


def _physical_crit_chance(all_fields: dict[str, str | float | bool], stats: dict[str, float]) -> float:
    engine_keys = ("combat_physcrit", "combat_generic_crit", "combat.physcrit", "stats.cun", "stats.lck")
    if any(key in all_fields for key in engine_keys):
        return max(
            0.0,
            _number_field(all_fields, "combat_physcrit")
            + _number_field(all_fields, "combat_generic_crit")
            + _crit_stat_bonus(stats)
            + _number_field(all_fields, "combat.physcrit", 1.0),
        )
    return _number_field(all_fields, "combat.crit")


def _physical_crit_power_bonus(all_fields: dict[str, str | float | bool]) -> float:
    return _number_field(all_fields, "combat_critical_power") + _number_field(all_fields, "combat.crit_power")


def _string_field(all_fields: dict[str, str | float | bool], key: str) -> str:
    value = all_fields.get(key)
    return value.strip() if isinstance(value, str) else ""


def _truthy_field(all_fields: dict[str, str | float | bool], key: str) -> bool:
    value = all_fields.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0.0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "nil"}
    return False


def _accuracy_effect_from_fields(all_fields: dict[str, str | float | bool]) -> str:
    return _string_field(all_fields, "combat.accuracy_effect") or _string_field(all_fields, "combat.talented")


def _number_fields_by_prefix(all_fields: dict[str, str | float | bool], prefix: str) -> dict[str, float]:
    return {
        key.removeprefix(prefix).lower(): float(value)
        for key, value in all_fields.items()
        if key.startswith(prefix) and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _damage_fields_by_prefixes(all_fields: dict[str, str | float | bool], *prefixes: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in all_fields.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for prefix in prefixes:
            if not key.startswith(prefix):
                continue
            damage_type = cm.normalize_damage_type(key.removeprefix(prefix))
            out[damage_type] = out.get(damage_type, 0.0) + float(value)
            break
    return out


def _resource_fields(all_fields: dict[str, str | float | bool]) -> dict[str, float]:
    resources: dict[str, float] = {}
    for key in _RESOURCE_COST_FIELDS:
        value = all_fields.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            resources[key] = float(value)
    return resources


def _melee_damage(all_fields: dict[str, str | float | bool], weapon_damage: float) -> float:
    stats = _number_fields_by_prefix(all_fields, "stats.")
    dammod = _number_fields_by_prefix(all_fields, "combat.dammod.")
    if not stats and _number_field(all_fields, "combat_dam") <= 0.0:
        return weapon_damage
    return cm.estimate_combat_damage(
        weapon_damage,
        combat_dam=_number_field(all_fields, "combat_dam"),
        stats=stats,
        dammod=dammod or None,
    )


@dataclass(slots=True)
class ThreatReport:
    weapon_threat_pct: float
    """Final threat score: percent of effective HP after hit-rate and
    rank/speed risk scalars."""

    hit_rate_pct: float
    expected_damage: float
    """Expected damage for one connecting hit, before rank/speed risk scalars."""

    peak_damage: float
    """Largest plausible one-hit damage if the weapon crits."""

    burst_expected_damage: float
    """Expected same-action weapon burst damage, before rank/speed risk scalars."""

    burst_peak_damage: float
    """Largest plausible same-action weapon burst if all direct weapon hits crit."""

    burst_hits: int
    can_burst_kill: bool
    raw_damage: float
    crit_chance_pct: float
    crit_used_pct: float
    """Crit chance actually used in the expected-damage calc (doubled
    for safety, clamped at 100)."""

    can_one_shot: bool
    damage_type: str
    damage_types: tuple[str, ...]
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


def _accuracy_effect_bonus(enemy: EnemyOffense, player: PlayerDefenses, scale: float, cap: float) -> float:
    scale_factor = 0.5 if enemy.accuracy_effect_scale else 1.0
    return min(cap, max(0.0, enemy.atk - player.defense) * scale * scale_factor)


def weapon_damage_after_accuracy(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Weapon base damage after ToME's mace accuracy bonus."""
    if enemy.accuracy_effect.lower() == "mace":
        return enemy.dam * (1.0 + _accuracy_effect_bonus(enemy, player, 0.002, 0.2))
    return enemy.dam


def weapon_apr_after_accuracy(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Weapon armor penetration after ToME's knife accuracy bonus."""
    if enemy.accuracy_effect.lower() == "knife":
        return enemy.apr * (1.0 + _accuracy_effect_bonus(enemy, player, 0.005, 0.5))
    return enemy.apr


def weapon_damage_rolls_after_accuracy(enemy: EnemyOffense, player: PlayerDefenses) -> tuple[float, float]:
    """Low/high weapon damage rolls after ToME's damage range and mace accuracy bonus."""
    base = weapon_damage_after_accuracy(enemy, player)
    ranged = base * max(0.0, enemy.damage_range or 1.0)
    return min(base, ranged), max(base, ranged)


def weapon_after_armor_expected_peak(enemy: EnemyOffense, player: PlayerDefenses) -> tuple[float, float]:
    """Expected/peak post-armor weapon damage after range, mace, and knife accuracy effects."""
    low_damage, high_damage = weapon_damage_rolls_after_accuracy(enemy, player)
    weapon_apr = weapon_apr_after_accuracy(enemy, player)
    low_after = cm.armor_absorb(low_damage, player.armor, player.armor_hardiness_pct, weapon_apr)
    high_after = cm.armor_absorb(high_damage, player.armor, player.armor_hardiness_pct, weapon_apr)
    return (low_after + high_after) / 2.0, max(low_after, high_after)


def weapon_crit_chance_pct(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Engine-style physical weapon crit chance after player reduction and axe accuracy bonus."""
    chance = enemy.crit_chance_pct - max(0.0, player.combat_crit_reduction_pct)
    if enemy.accuracy_effect.lower() == "axe":
        chance += _accuracy_effect_bonus(enemy, player, 0.25, 25.0)
    return max(0.0, min(100.0, chance))


def weapon_crit_power_multiplier(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Engine-style physical weapon crit multiplier, including sword accuracy bonus."""
    crit_power = enemy.crit_power_bonus_pct / 100.0 + cm.DEFAULT_CRIT_POWER
    if enemy.accuracy_effect.lower() == "sword":
        crit_power += _accuracy_effect_bonus(enemy, player, 0.004, 0.4)
    if player.ignore_direct_crits_pct > 0:
        ignore = max(0.0, min(1.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    return crit_power


def weapon_proc_damage_multiplier(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Damage multiplier ToME applies to melee projectors for staff accuracy effects."""
    if enemy.accuracy_effect.lower() == "staff":
        return 1.0 + _accuracy_effect_bonus(enemy, player, 0.02, 2.0)
    return 1.0


def weapon_project_damage(
    enemy: EnemyOffense,
    player: PlayerDefenses,
    damages: dict[str, float],
    *,
    proc_multiplier: float | None = None,
) -> float:
    multiplier = weapon_proc_damage_multiplier(enemy, player) if proc_multiplier is None else proc_multiplier
    total = 0.0
    for raw_type, raw_damage in damages.items():
        if raw_damage <= 0.0:
            continue
        damage_type = cm.normalize_damage_type(raw_type)
        resist_mult = cm.resist_multiplier_for_type(
            player.resists,
            enemy.resists_pen,
            player.resists_cap,
            damage_type,
        )
        damage_inc = cm.damage_increase_for_type(enemy.inc_damage, damage_type)
        total += raw_damage * multiplier * resist_mult * (1.0 + damage_inc / 100.0)
    return total


def weapon_project_damage_expected_peak(
    enemy: EnemyOffense,
    player: PlayerDefenses,
    crit_used_pct: float,
    *,
    burst_hits: int = 1,
) -> tuple[float, float]:
    proc_multiplier = weapon_proc_damage_multiplier(enemy, player)
    on_hit = weapon_project_damage(enemy, player, enemy.melee_project, proc_multiplier=proc_multiplier)
    on_hit += weapon_project_damage(enemy, player, enemy.burst_on_hit, proc_multiplier=proc_multiplier)
    on_crit = weapon_project_damage(enemy, player, enemy.burst_on_crit, proc_multiplier=proc_multiplier)
    expected = on_hit + on_crit * max(0.0, min(100.0, crit_used_pct)) / 100.0
    peak = on_hit + (on_crit if crit_used_pct > 0.0 else 0.0)
    hits = max(1, burst_hits)
    return expected * hits, peak * hits


def weapon_damage_types(enemy: EnemyOffense) -> tuple[str, ...]:
    """Return base weapon and proc damage types in the order they can appear."""
    damage_types: list[str] = []
    _append_unique_damage_type(damage_types, enemy.damage_type)
    for table in (enemy.melee_project, enemy.burst_on_hit, enemy.burst_on_crit):
        for raw_type, raw_damage in table.items():
            if raw_damage > 0.0:
                _append_unique_damage_type(damage_types, raw_type)
    return tuple(damage_types)


def _append_unique_damage_type(damage_types: list[str], raw_type: str) -> None:
    damage_type = cm.normalize_damage_type(raw_type)
    if damage_type not in damage_types:
        damage_types.append(damage_type)


def weapon_threat(enemy: EnemyOffense, player: PlayerDefenses) -> ThreatReport:
    """Compute a single-hit weapon threat report.

    Mirrors `calcWeaponThreat` in the addon where useful, but uses the
    engine's type-specific damage/resist pipeline for the actual weapon
    damage type.
    """
    damage_type = cm.normalize_damage_type(enemy.damage_type)
    damage_types = weapon_damage_types(enemy)
    hit = cm.hit_rate(enemy.atk, player.defense, player.evasion_pct)
    after_armor, after_armor_peak = weapon_after_armor_expected_peak(enemy, player)
    resist_mult = cm.resist_multiplier_for_type(
        player.resists,
        enemy.resists_pen,
        player.resists_cap,
        damage_type,
    )
    damage_inc = cm.damage_increase_for_type(enemy.inc_damage, damage_type)
    daminc_mult = 1.0 + damage_inc / 100.0

    crit_chance = weapon_crit_chance_pct(enemy, player)
    crit_doubled = min(100.0, crit_chance * 2.0)
    crit_power = weapon_crit_power_multiplier(enemy, player)
    crit_mult = cm.crit_expected_multiplier(crit_doubled, crit_power)
    weapon_mults = enemy.weapon_multipliers_against(player)
    burst_hits = max(1, weapon_mults.burst_hits)
    project_expected, project_peak = weapon_project_damage_expected_peak(enemy, player, crit_doubled)
    burst_project_expected, burst_project_peak = weapon_project_damage_expected_peak(
        enemy,
        player,
        crit_doubled,
        burst_hits=burst_hits,
    )

    base_multiplier = after_armor * resist_mult * daminc_mult
    base_peak_multiplier = after_armor_peak * resist_mult * daminc_mult
    base_hit = base_multiplier * max(1.0, weapon_mults.max_hit)
    base_hit_peak = base_peak_multiplier * max(1.0, weapon_mults.max_hit)
    base_burst = base_multiplier * max(1.0, weapon_mults.burst)
    base_burst_peak = base_peak_multiplier * max(1.0, weapon_mults.burst)
    expected = base_hit * crit_mult + project_expected
    peak = base_hit_peak * crit_power if crit_chance > 0.0 and crit_power > 1.0 else base_hit_peak
    peak += project_peak
    burst_expected = base_burst * crit_mult + burst_project_expected
    burst_peak = base_burst_peak * crit_power if crit_chance > 0.0 and crit_power > 1.0 else base_burst_peak
    burst_peak += burst_project_peak

    threat_damage = max(expected, burst_expected)
    if enemy.rank > RANK_BOSS_THRESHOLD:
        threat_damage *= RANK_BOSS_SCALAR
    if enemy.global_speed > 1.0:
        threat_damage *= enemy.global_speed
    weapon_action_rate = _weapon_action_rate(enemy.physspeed)
    if weapon_action_rate > 1.0:
        threat_damage *= weapon_action_rate

    threat_pct = (threat_damage / player.effective_hp) * 100.0
    if threat_pct < HIGH_THREAT_DOUBLE_HITRATE_PIVOT:
        threat_pct *= hit / 100.0
    else:
        threat_pct *= min(100.0, hit * 2.0) / 100.0

    notes: list[str] = []
    low_roll, high_roll = weapon_damage_rolls_after_accuracy(enemy, player)
    mace_bonus = _accuracy_effect_bonus(enemy, player, 0.002, 0.2) if enemy.accuracy_effect.lower() == "mace" else 0.0
    knife_bonus = _accuracy_effect_bonus(enemy, player, 0.005, 0.5) if enemy.accuracy_effect.lower() == "knife" else 0.0
    staff_bonus = weapon_proc_damage_multiplier(enemy, player) - 1.0
    if peak >= player.effective_hp:
        notes.append(f"Can one-shot you ({peak:.0f} peak damage vs {player.effective_hp:.0f} effective HP)")
    elif burst_peak >= player.effective_hp and burst_hits > 1:
        notes.append(
            f"Can kill with a {burst_hits}-hit weapon burst "
            f"({burst_peak:.0f} peak damage vs {player.effective_hp:.0f} effective HP)"
        )
    elif expected >= player.effective_hp * 0.7:
        notes.append(f"Can remove ~{expected / player.effective_hp * 100:.0f}% HP per hit")
    elif burst_expected >= player.effective_hp * 0.7 and burst_hits > 1:
        notes.append(f"Can remove ~{burst_expected / player.effective_hp * 100:.0f}% HP in a weapon burst")
    if burst_hits > 1:
        notes.append(f"Strongest weapon talent chains {burst_hits} direct hits")
    if high_roll > low_roll:
        notes.append(f"Weapon damage range: {low_roll:.0f}-{high_roll:.0f} before armor")
    if mace_bonus > 0.0:
        notes.append(f"Mace accuracy bonus: +{mace_bonus * 100.0:.0f}% base damage")
    if knife_bonus > 0.0:
        notes.append(f"Knife accuracy bonus: +{knife_bonus * 100.0:.0f}% armor penetration")
    if project_expected > 0.0:
        notes.append(f"On-hit project adds ~{project_expected:.0f} damage")
        project_types = tuple(type_name for type_name in damage_types if type_name != damage_type)
        if project_types:
            notes.append(f"Project damage types: {', '.join(project_types)}")
    if staff_bonus > 0.0:
        notes.append(f"Staff accuracy bonus: +{staff_bonus * 100.0:.0f}% project damage")
    if hit >= 75:
        notes.append(f"Very likely to hit ({hit:.0f}%)")
    elif hit < 25:
        notes.append(f"Unlikely to hit ({hit:.0f}%)")
    if damage_inc >= 25:
        notes.append(f"Boosted {damage_type} damage: +{damage_inc:.0f}%")
    if enemy.global_speed > 1.0:
        notes.append(f"Acts {enemy.global_speed:.1f}x per turn")
    if weapon_action_rate > 1.0:
        notes.append(f"Fast weapon action ({weapon_action_rate:.1f}x rate)")

    return ThreatReport(
        weapon_threat_pct=round(threat_pct, 1),
        hit_rate_pct=round(hit, 1),
        expected_damage=round(expected, 1),
        peak_damage=round(peak, 1),
        burst_expected_damage=round(burst_expected, 1),
        burst_peak_damage=round(burst_peak, 1),
        burst_hits=burst_hits,
        can_burst_kill=burst_peak >= player.effective_hp and burst_hits > 1,
        raw_damage=round(enemy.dam, 1),
        crit_chance_pct=round(crit_chance, 1),
        crit_used_pct=crit_doubled,
        can_one_shot=peak >= player.effective_hp,
        damage_type=damage_type,
        damage_types=damage_types,
        worst_resist_type=damage_type,
        worst_resist_multiplier=round(resist_mult, 3),
        best_inc_type=damage_type,
        best_inc_pct=damage_inc,
        notes=notes,
    )


def _weapon_action_rate(physspeed: float) -> float:
    return 1.0 / max(physspeed or 1.0, 0.1)
