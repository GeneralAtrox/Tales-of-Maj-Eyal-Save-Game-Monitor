"""Combat formula primitives — ported from the ToME engine and the
Danger Alert addon (yutio888, 2019).

Pure functions. No dependencies on memory-reader or character-sheet
data structures. All inputs are plain floats in their natural game
units (percent for rates/resists, raw for damage/armor).
"""

from __future__ import annotations

from typing import Final

# ── ToME constants ──────────────────────────────────────────────────────────

RESIST_CAP: Final[float] = 70.0
"""Default cap on effective resistance (post-penetration)."""

RESIST_HARD_CAP: Final[float] = 100.0
"""Absolute ceiling some content raises the cap to."""

HIT_RATE_BASE: Final[float] = 50.0
HIT_RATE_SLOPE: Final[float] = 2.5
"""`hit_rate = 50 + 2.5 * (atk - def)` — canonical ToME formula."""

DEFAULT_CRIT_POWER: Final[float] = 1.5
"""Base crit multiplier before `combat_critical_power` bonuses."""


# ── Primitives ──────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def hit_rate(attacker_atk: float, defender_def: float, evasion_pct: float = 0.0) -> float:
    """Chance (0..100) that the attacker's swing connects.

    Evasion is composed post-hit-rate as an independent filter.
    """
    base = HIT_RATE_BASE + HIT_RATE_SLOPE * (attacker_atk - defender_def)
    base = _clamp(base, 0.0, 100.0)
    return base * (100.0 - _clamp(evasion_pct, 0.0, 100.0)) / 100.0


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


def effective_resist_multiplier(
    resist_pct: float,
    resist_pen_pct: float = 0.0,
    cap: float = RESIST_CAP,
) -> float:
    """Damage multiplier after resistance and penetration.

    Returns a value in roughly [0, 2]. 1.0 means full damage, 0.3 means
    70% resisted. Negative resist amplifies damage above 1.0.
    """
    pen = _clamp(resist_pen_pct, 0.0, 100.0) / 100.0
    effective = _clamp(resist_pct * (1.0 - pen), -100.0, cap)
    return 1.0 - effective / 100.0


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
    cap: float = RESIST_CAP,
) -> tuple[str, float]:
    """Given a player's resist table, find the damage type they resist
    least effectively — the attacker's best bet.

    Returns `(damage_type, multiplier)`. Uses `"all"` as the floor.
    """
    resists_pen = resists_pen or {}
    all_r = resists.get("all", 0.0)
    best_type = "all"
    best_mult = effective_resist_multiplier(all_r, resists_pen.get("all", 0.0), cap)
    for dtype, r in resists.items():
        if dtype == "all":
            continue
        pen = resists_pen.get(dtype, resists_pen.get("all", 0.0))
        mult = effective_resist_multiplier(r, pen, cap)
        if mult > best_mult:
            best_mult = mult
            best_type = dtype
    return best_type, best_mult


def best_damage_increase(inc_damage: dict[str, float]) -> tuple[str, float]:
    """Attacker's largest damage-type bonus: `max("all", per-type)`.

    Returns `(damage_type, inc_pct)`. Mirrors the addon's convention
    of using the biggest available bonus rather than matching types.
    """
    if not inc_damage:
        return "all", 0.0
    all_i = inc_damage.get("all", 0.0)
    best_type = "all"
    best_val = all_i
    for dtype, v in inc_damage.items():
        if dtype == "all":
            continue
        if v > best_val:
            best_val = v
            best_type = dtype
    return best_type, best_val
