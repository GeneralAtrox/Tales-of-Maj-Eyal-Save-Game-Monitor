"""Inverse scoring: smallest defensive lever that turns a one-shot into
a survivable hit.

Given a `ThreatReport` that exceeds effective HP, propose concrete
player-side deltas — "increase fire resist by 8%", "gain 12 armor" —
that would bring `expected_damage` below the threshold.

Each lever is computed analytically from the same formulas in
`combat_math`. When the math says a lever cannot save you (e.g. raw
damage is so high that even hitting the resist cap leaves you dead),
that lever is omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from . import combat_math as cm
from .enemy_threat import EnemyOffense, PlayerDefenses, ThreatReport

SURVIVAL_HP_FRACTION: Final[float] = 0.95
"""Target: drop expected damage to ≤95% of effective HP (a little room
to breathe, not razor's edge)."""


@dataclass(slots=True)
class AdviceItem:
    lever: str
    """Short label: 'fire resist', 'armor', 'armor hardiness', 'max HP'."""

    description: str
    """Human-readable: 'Raise fire resistance to 34% (+8 from current)'."""

    delta: float
    """Amount of change needed in the lever's native unit."""

    target_value: float
    """Absolute value the lever reaches after the change."""

    feasible: bool
    """False when the lever hits its cap before reaching survival."""


def _crit_multiplier(enemy: EnemyOffense, player: PlayerDefenses, *, peak: bool = False) -> float:
    crit_chance = max(0.0, min(100.0, enemy.crit_chance_pct))
    crit_power = enemy.crit_power_bonus_pct / 100.0 + cm.DEFAULT_CRIT_POWER
    if player.ignore_direct_crits_pct > 0:
        ignore = max(0.0, min(1.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    if peak:
        return crit_power if crit_chance > 0.0 and crit_power > 1.0 else 1.0
    crit_doubled = min(100.0, crit_chance * 2.0)
    return cm.crit_expected_multiplier(crit_doubled, crit_power)


def _raw_resist_for_effective(
    all_resist: float,
    needed_effective: float,
) -> float:
    """Solve ToME's all+specific stacking for a specific raw resist."""
    all_factor = min(all_resist / 100.0, 1.0)
    denom = 1.0 - all_factor
    if denom <= 0.0:
        return 0.0
    needed_factor = 1.0 - (1.0 - needed_effective / 100.0) / denom
    return needed_factor * 100.0


def _peak_hit(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Replay the core of `weapon_threat` to get peak one-hit damage.

    Hit rate and rank/speed affect the danger tier, not whether one
    connecting crit can kill the player.
    """
    after_armor = cm.armor_absorb(
        enemy.dam, player.armor, player.armor_hardiness_pct, enemy.apr
    )
    damage_type = cm.normalize_damage_type(enemy.damage_type)
    resist_mult = cm.resist_multiplier_for_type(
        player.resists,
        enemy.resists_pen,
        player.resists_cap,
        damage_type,
    )
    damage_inc = cm.damage_increase_for_type(enemy.inc_damage, damage_type)
    daminc_mult = 1.0 + damage_inc / 100.0
    crit_mult = _crit_multiplier(enemy, player, peak=True)

    return after_armor * crit_mult * resist_mult * daminc_mult * max(1.0, enemy.talent_max_weapon_mult)


def survive_one_hit_advice(
    enemy: EnemyOffense,
    player: PlayerDefenses,
    target_fraction: float = SURVIVAL_HP_FRACTION,
) -> list[AdviceItem]:
    """Return levers that would bring one hit below survival threshold.

    Sorted by `delta` (smallest first). Empty list means already safe.
    """
    current = _peak_hit(enemy, player)
    target_dam = player.effective_hp * target_fraction
    if current <= target_dam:
        return []

    advice: list[AdviceItem] = []
    after_armor = cm.armor_absorb(
        enemy.dam, player.armor, player.armor_hardiness_pct, enemy.apr
    )

    # Crit/inc_damage wrappers that are invariant under the player's armor/resist
    # changes — factor them out so lever math is clean.
    damage_type = cm.normalize_damage_type(enemy.damage_type)
    crit_mult = _crit_multiplier(enemy, player, peak=True)
    damage_inc = cm.damage_increase_for_type(enemy.inc_damage, damage_type)
    daminc_mult = 1.0 + damage_inc / 100.0
    tal_mult = max(1.0, enemy.talent_max_weapon_mult)
    wrapper = crit_mult * daminc_mult * tal_mult

    # ── Lever 1: resistance on the actual weapon damage type ──────────────
    current_effective = cm.effective_resist_pct(
        player.resists,
        damage_type,
        player.resists_cap,
    )
    current_raw = player.resists.get(damage_type, player.resists.get(damage_type.lower(), 0.0))
    all_resist = player.resists.get("all", player.resists.get("ALL", 0.0))
    pen = cm.resist_pen_for_type(enemy.resists_pen, damage_type)
    cap = cm.resist_cap_for_type(player.resists_cap, damage_type)
    # expected = after_armor * wrapper * (1 - effective/100) <= target_dam
    # effective >= (1 - target_dam / (after_armor * wrapper)) * 100
    denom = after_armor * wrapper
    if denom > 0:
        needed_effective = (1.0 - target_dam / denom) * 100.0
        pen_factor = 1.0 - min(100.0, max(0.0, pen)) / 100.0
        if needed_effective > 0.0 and pen_factor <= 0.0:
            delta = max(0.0, cap - current_effective)
            if delta > 0:
                advice.append(
                    AdviceItem(
                        lever=f"{damage_type} resist",
                        description=(
                            f"Raise {damage_type} effective resistance as far as possible "
                            f"(cap {cap:.0f}%, +{delta:.0f} from {current_effective:.0f}%)"
                            " -- not feasible alone because the enemy fully penetrates it"
                        ),
                        delta=round(delta, 1),
                        target_value=round(cap, 1),
                        feasible=False,
                    )
                )
        else:
            needed_combined = needed_effective
            if needed_effective > 0.0:
                needed_combined = needed_effective / pen_factor
            if damage_type == "all":
                needed_raw_resist = needed_combined
            else:
                needed_raw_resist = _raw_resist_for_effective(all_resist, needed_combined)
            feasible = needed_combined <= cap + 1e-6
            delta = max(0.0, needed_raw_resist - current_raw)
            if delta > 0:
                advice.append(
                    AdviceItem(
                        lever=f"{damage_type} resist",
                        description=(
                            f"Raise {damage_type} resistance to "
                            f"{needed_raw_resist:.0f}% (+{delta:.0f} from {current_raw:.0f}%)"
                            + ("" if feasible else f" -- exceeds {cap:.0f}% cap, not feasible alone")
                        ),
                        delta=round(delta, 1),
                        target_value=round(needed_raw_resist, 1),
                        feasible=feasible,
                    )
                )

    # ── Lever 2: armor (only meaningful for the hardened portion) ─────────
    hard_pct = max(0.0, min(100.0, player.armor_hardiness_pct)) / 100.0
    if hard_pct > 0:
        resist_mult = cm.resist_multiplier_for_type(
            player.resists,
            enemy.resists_pen,
            player.resists_cap,
            damage_type,
        )
        # Fixed portion (soft damage) can't be armored away.
        soft_dam = enemy.dam * (1.0 - hard_pct)
        soft_after_resist = soft_dam * resist_mult * wrapper
        # Need: (hardened_after_armor + soft_dam) * resist_mult * wrapper <= target_dam
        # hardened_after_armor <= target_dam/(resist_mult*wrapper) - soft_dam
        budget = target_dam / (resist_mult * wrapper) - soft_dam if resist_mult * wrapper > 0 else -1
        if budget < 0:
            advice.append(
                AdviceItem(
                    lever="armor",
                    description="Armor alone can't save you -- unarmored damage already exceeds survival budget",
                    delta=0.0,
                    target_value=player.armor,
                    feasible=False,
                )
            )
        else:
            # hardened_after_armor = max(raw*H - (A-apr), 0)
            # for it to be <= budget: A >= raw*H - budget + apr, floored at current
            needed_armor = enemy.dam * hard_pct - budget + enemy.apr
            needed_armor = max(0.0, needed_armor)
            armor_delta = max(0.0, needed_armor - player.armor)
            if armor_delta > 0 and soft_after_resist <= target_dam:
                advice.append(
                    AdviceItem(
                        lever="armor",
                        description=(
                            f"Raise armor to {needed_armor:.0f} "
                            f"(+{armor_delta:.0f} from {player.armor:.0f})"
                        ),
                        delta=round(armor_delta, 1),
                        target_value=round(needed_armor, 1),
                        feasible=True,
                    )
                )

    # ── Lever 3: raw HP (always works, but usually large delta) ───────────
    # Need effective_hp * target_fraction >= current  →  max_life >= current/target_fraction + die_at
    needed_max_life = current / target_fraction + player.die_at
    hp_delta = max(0.0, needed_max_life - player.max_life)
    if hp_delta > 0:
        advice.append(
            AdviceItem(
                lever="max HP",
                description=f"Raise max HP to {needed_max_life:.0f} (+{hp_delta:.0f})",
                delta=round(hp_delta, 1),
                target_value=round(needed_max_life, 1),
                feasible=True,
            )
        )

    advice.sort(key=lambda a: (not a.feasible, a.delta))
    return advice


def advice_for_report(
    report: ThreatReport,
    enemy: EnemyOffense,
    player: PlayerDefenses,
    target_fraction: float = SURVIVAL_HP_FRACTION,
) -> list[AdviceItem]:
    """Convenience: skip the work when the report says we're already fine."""
    if not report.can_one_shot and report.weapon_threat_pct < 70:
        return []
    return survive_one_hit_advice(enemy, player, target_fraction)
