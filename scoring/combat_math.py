"""Combat formula primitives — ported from the ToME engine and the
Danger Alert addon (yutio888, 2019).

Pure functions. No dependencies on memory-reader or character-sheet
data structures. All inputs are plain floats in their natural game
units (percent for rates/resists, raw for damage/armor).
"""

from __future__ import annotations

import math
from typing import Final

# ── ToME constants ──────────────────────────────────────────────────────────

RESIST_CAP: Final[float] = 70.0
"""Default cap on resistance before penetration."""

RESIST_HARD_CAP: Final[float] = 100.0
"""Absolute ceiling some content raises the cap to."""

HIT_RATE_BASE: Final[float] = 50.0
HIT_RATE_SLOPE: Final[float] = 2.5
"""`hit_rate = 50 + 2.5 * (atk - def)` — canonical ToME formula."""

DEFAULT_CRIT_POWER: Final[float] = 1.5
"""Base crit multiplier before `combat_critical_power` bonuses."""

DEFAULT_DAMAGE_TYPE: Final[str] = "PHYSICAL"
DEFAULT_DAMMOD: Final[dict[str, float]] = {"str": 0.6}


# ── Primitives ──────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def normalize_damage_type(damage_type: str | None, default: str = DEFAULT_DAMAGE_TYPE) -> str:
    """Return the canonical key used by ToME damage tables."""
    if damage_type is None:
        return default
    key = str(damage_type).strip()
    if not key:
        return default
    if key.lower() == "all":
        return "all"
    return key.upper()


def _table_value(table: dict[str, float] | None, damage_type: str) -> float:
    if not table:
        return 0.0
    key = normalize_damage_type(damage_type, "all")
    if key in table:
        return float(table[key])
    if key == "all":
        return float(table.get("ALL", 0.0))
    return float(table.get(key.lower(), 0.0))


def hit_rate(attacker_atk: float, defender_def: float, evasion_pct: float = 0.0) -> float:
    """Chance (0..100) that the attacker's swing connects.

    Evasion is composed post-hit-rate as an independent filter.
    """
    base = math.ceil(HIT_RATE_BASE + HIT_RATE_SLOPE * (attacker_atk - defender_def))
    base = _clamp(base, 0.0, 100.0)
    return base * (100.0 - _clamp(evasion_pct, 0.0, 100.0)) / 100.0


def rescale_damage(raw_damage: float) -> float:
    """ToME's `rescaleDamage` curve for positive damage values."""
    if raw_damage <= 0.0:
        return raw_damage
    return raw_damage**1.04


def rescale_combat_stats(raw_value: float, interval: float = 20.0, step: float = 1.0) -> float:
    """ToME's diminishing-returns `rescaleCombatStats` function."""
    result = raw_value
    shift = 1.0 + step
    tier = interval
    base = interval
    while True:
        next_result = tier + (raw_value - base) / shift
        if next_result < result:
            result = next_result
            base += interval * shift
            tier += interval
            shift += step
        else:
            return math.floor(result)


def combat_damage_power(weapon_damage: float) -> float:
    """ToME's weapon-power portion of `combatDamage`."""
    power = max(weapon_damage, 1.0)
    return (math.sqrt(power / 10.0) - 1.0) * 0.5 + 1.0


def estimate_combat_damage(
    weapon_damage: float,
    *,
    combat_dam: float = 0.0,
    stats: dict[str, float] | None = None,
    dammod: dict[str, float] | None = None,
) -> float:
    """Estimate ToME `combatDamage` for a weapon-style melee hit.

    This intentionally omits talent hooks and training bonuses, but it
    captures the core engine path: physical power, stat dammod, weapon
    power, and final damage rescaling.
    """
    if weapon_damage <= 0.0:
        return 0.0
    stats = stats or {}
    dammod = dammod or DEFAULT_DAMMOD
    stat_total = sum(float(stats.get(stat, 0.0)) * float(mod) for stat, mod in dammod.items())
    physical_raw = max(0.0, float(combat_dam) + float(stats.get("str", 0.0)))
    physical_power = rescale_combat_stats(physical_raw)
    stat_mod = rescale_combat_stats(stat_total, 45.0, 1.0 / 3.0)
    raw = 0.3 * (physical_power + stat_mod) * combat_damage_power(weapon_damage)
    return rescale_damage(raw)


def sp_multiply(a_pct: float, b_pct: float) -> float:
    """Stack two independent 0..100 percent sources as probabilities.

    `sp_multiply(50, 50) == 75`, not 100. Use for composing resist,
    evasion, or CC-immunity contributions that should not simply add.
    """
    a = _clamp(a_pct, 0.0, 100.0)
    b = _clamp(b_pct, 0.0, 100.0)
    return 100.0 - (100.0 - a) * (100.0 - b) / 100.0


def armor_absorb(
    raw_dam: float,
    armor: float,
    hardiness_pct: float,
    apr: float = 0.0,
) -> float:
    """Apply ToME's armor/hardiness split to raw damage.

    `hardiness_pct%` of the damage is absorbed by armor (subtractively,
    floored at 0); the remainder bypasses armor entirely. APR reduces
    effective armor. Note: armor cannot reduce damage to zero unless
    hardiness is 100%.
    """
    eff_armor = max(0.0, armor - apr)
    hard = _clamp(hardiness_pct, 0.0, 100.0) / 100.0
    hardened = max(0.0, raw_dam * hard - eff_armor)
    soft = raw_dam * (1.0 - hard)
    return hardened + soft


def resist_cap_for_type(
    resists_cap: dict[str, float] | None,
    damage_type: str,
    default_cap: float = RESIST_CAP,
) -> float:
    """Resolve ToME's effective resist ceiling for one damage type.

    `resists_cap` mirrors the engine layout: `"all"` is the global cap
    and a specific entry adds on top for that damage type.
    """
    if not resists_cap:
        return default_cap
    damage_type = normalize_damage_type(damage_type, "all")
    all_cap = float(resists_cap.get("all", resists_cap.get("ALL", default_cap)))
    if damage_type == "all":
        return _clamp(all_cap, -100.0, RESIST_HARD_CAP)
    return _clamp(all_cap + _table_value(resists_cap, damage_type), -100.0, RESIST_HARD_CAP)


def effective_resist_multiplier(
    resist_pct: float,
    resist_pen_pct: float = 0.0,
    cap: float = RESIST_CAP,
) -> float:
    """Damage multiplier after resistance and penetration.

    Returns a value in roughly [0, 2]. 1.0 means full damage, 0.3 means
    70% resisted. Negative resist amplifies damage above 1.0.
    """
    effective = _clamp(resist_pct, -100.0, cap)
    if effective > 0.0:
        pen = _clamp(resist_pen_pct, 0.0, 100.0) / 100.0
        effective *= 1.0 - pen
    return 1.0 - effective / 100.0


def effective_resist_pct(
    resists: dict[str, float] | None,
    damage_type: str,
    resists_cap: dict[str, float] | None = None,
) -> float:
    """Engine-style `combatGetResist` for one damage type.

    ToME stacks `all` resistance and the specific damage type as
    independent reductions, then bounds the combined result by the
    type's effective cap.
    """
    damage_type = normalize_damage_type(damage_type, "all")
    all_r = _table_value(resists, "all")
    if damage_type == "all":
        specific_r = 0.0
    else:
        specific_r = _table_value(resists, damage_type)
    all_factor = min(all_r / 100.0, 1.0)
    specific_factor = min(specific_r / 100.0, 1.0)
    combined = 100.0 * (1.0 - (1.0 - all_factor) * (1.0 - specific_factor))
    return _clamp(combined, -100.0, resist_cap_for_type(resists_cap, damage_type))


def resist_pen_for_type(resists_pen: dict[str, float] | None, damage_type: str) -> float:
    """Engine-style `combatGetResistPen` for one damage type."""
    damage_type = normalize_damage_type(damage_type)
    pen = _table_value(resists_pen, "all")
    if damage_type != "all":
        pen += _table_value(resists_pen, damage_type)
    return min(pen, RESIST_CAP)


def resist_multiplier_for_type(
    resists: dict[str, float] | None,
    resists_pen: dict[str, float] | None,
    resists_cap: dict[str, float] | None,
    damage_type: str,
) -> float:
    """Damage multiplier after ToME resist stacking and penetration."""
    resist = effective_resist_pct(resists, damage_type, resists_cap)
    pen = resist_pen_for_type(resists_pen, damage_type)
    return effective_resist_multiplier(resist, pen, resist_cap_for_type(resists_cap, damage_type))


def damage_increase_for_type(inc_damage: dict[str, float] | None, damage_type: str) -> float:
    """Engine-style `combatGetDamageIncrease` for one damage type."""
    damage_type = normalize_damage_type(damage_type)
    inc = _table_value(inc_damage, "all")
    if damage_type != "all":
        inc += _table_value(inc_damage, damage_type)
    return inc


def crit_expected_multiplier(
    crit_chance_pct: float,
    crit_power_mult: float = DEFAULT_CRIT_POWER,
) -> float:
    """Expected damage multiplier from crit.

    `crit_power_mult` is the crit multiplier itself, not a bonus
    (e.g. 1.5 = +50% on crit). Caller is responsible for any
    pessimism (like doubling crit chance for safety); this function
    just does the math.
    """
    c = _clamp(crit_chance_pct, 0.0, 100.0) / 100.0
    return c * crit_power_mult + (1.0 - c)


def worst_damage_multiplier(
    resists: dict[str, float],
    resists_pen: dict[str, float] | None = None,
    resists_cap: dict[str, float] | None = None,
) -> tuple[str, float]:
    """Given a player's resist table, find the damage type they resist
    least effectively — the attacker's best bet.

    Returns `(damage_type, multiplier)`. Uses `"all"` as the floor.
    """
    candidate_types = {
        normalize_damage_type(dtype, "all")
        for source in (resists, resists_pen or {})
        for dtype in source
        if normalize_damage_type(dtype, "all") != "all"
    }
    best_type = "all"
    best_mult = resist_multiplier_for_type(resists, resists_pen, resists_cap, "all")
    for dtype in sorted(candidate_types):
        if dtype == "all":
            continue
        mult = resist_multiplier_for_type(resists, resists_pen, resists_cap, dtype)
        if mult > best_mult:
            best_mult = mult
            best_type = dtype
    return best_type, best_mult


def best_damage_increase(inc_damage: dict[str, float]) -> tuple[str, float]:
    """Attacker's largest damage-type bonus using engine additivity.

    Returns `(damage_type, inc_pct)`. This remains conservative by
    searching all available types, but each candidate is computed as
    `all + per-type`, matching `combatGetDamageIncrease`.
    """
    if not inc_damage:
        return "all", 0.0
    best_type = "all"
    best_val = damage_increase_for_type(inc_damage, "all")
    for dtype in inc_damage:
        dtype = normalize_damage_type(dtype, "all")
        if dtype == "all":
            continue
        candidate = damage_increase_for_type(inc_damage, dtype)
        if candidate > best_val:
            best_val = candidate
            best_type = dtype
    return best_type, best_val
