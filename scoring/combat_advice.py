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


def _expected_hit(enemy: EnemyOffense, player: PlayerDefenses) -> float:
    """Replay the core of `weapon_threat` to get a raw expected damage
    number — without the hit-rate and rank/speed wrappers. This is the
    number we need to bring below effective HP; hit rate and rank only
    affect the *tier label*, not whether a single connecting hit kills.
    """
    after_armor = cm.armor_absorb(
        enemy.dam, player.armor, player.armor_hardiness_pct, enemy.apr
    )
    _, worst_mult = cm.worst_damage_multiplier(player.resists, player.resists_pen)
    _, best_inc = cm.best_damage_increase(enemy.inc_damage)
    daminc_mult = 1.0 + best_inc / 100.0

    crit_doubled = min(100.0, enemy.crit_chance_pct * 2.0)
    crit_power = enemy.crit_power_bonus_pct / 100.0 + cm.DEFAULT_CRIT_POWER
    if player.ignore_direct_crits_pct > 0:
        ignore = max(0.0, min(1.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    crit_mult = cm.crit_expected_multiplier(crit_doubled, crit_power)

    return after_armor * crit_mult * worst_mult * daminc_mult * max(1.0, enemy.talent_max_weapon_mult)


def survive_one_hit_advice(
    enemy: EnemyOffense,
    player: PlayerDefenses,
    target_fraction: float = SURVIVAL_HP_FRACTION,
) -> list[AdviceItem]:
    """Return levers that would bring one hit below survival threshold.

    Sorted by `delta` (smallest first). Empty list means already safe.
    """
    current = _expected_hit(enemy, player)
    target_dam = player.effective_hp * target_fraction
    if current <= target_dam:
        return []

    advice: list[AdviceItem] = []
    after_armor = cm.armor_absorb(
        enemy.dam, player.armor, player.armor_hardiness_pct, enemy.apr
    )

    # Crit/inc_damage wrappers that are invariant under the player's armor/resist
    # changes — factor them out so lever math is clean.
    crit_doubled = min(100.0, enemy.crit_chance_pct * 2.0)
    crit_power = enemy.crit_power_bonus_pct / 100.0 + cm.DEFAULT_CRIT_POWER
    if player.ignore_direct_crits_pct > 0:
        ignore = max(0.0, min(1.0, player.ignore_direct_crits_pct / 100.0))
        crit_power = 1.0 + (crit_power - 1.0) * (1.0 - ignore)
    crit_mult = cm.crit_expected_multiplier(crit_doubled, crit_power)
    _, best_inc = cm.best_damage_increase(enemy.inc_damage)
    daminc_mult = 1.0 + best_inc / 100.0
    tal_mult = max(1.0, enemy.talent_max_weapon_mult)
    wrapper = crit_mult * daminc_mult * tal_mult

    # ── Lever 1: resistance on the worst damage type ──────────────────────
    worst_type, _ = cm.worst_damage_multiplier(player.resists, player.resists_pen)
    current_resist = player.resists.get(worst_type, player.resists.get("all", 0.0))
    pen = player.resists_pen.get(worst_type, player.resists_pen.get("all", 0.0))
    # expected = after_armor * wrapper * (1 - effective/100) <= target_dam
    # effective >= (1 - target_dam / (after_armor * wrapper)) * 100
    denom = after_armor * wrapper
    if denom > 0:
        needed_effective = (1.0 - target_dam / denom) * 100.0
        # Undo penetration to get the raw resist we need on paper
        # effective = raw * (1 - pen/100) → raw = effective / (1 - pen/100)
        pen_factor = 1.0 - min(100.0, max(0.0, pen)) / 100.0
        if pen_factor > 0:
            needed_raw_resist = needed_effective / pen_factor
            feasible = needed_raw_resist <= cm.RESIST_CAP + 1e-6
            delta = max(0.0, needed_raw_resist - current_resist)
            if delta > 0:
                advice.append(
                    AdviceItem(
                        lever=f"{worst_type} resist",
                        description=(
                            f"Raise {worst_type} resistance to "
                            f"{needed_raw_resist:.0f}% (+{delta:.0f} from {current_resist:.0f}%)"
                            + ("" if feasible else f" -- exceeds {cm.RESIST_CAP:.0f}% cap, not feasible alone")
                        ),
                        delta=round(delta, 1),
                        target_value=round(needed_raw_resist, 1),
                        feasible=feasible,
                    )
                )

    # ── Lever 2: armor (only meaningful for the hardened portion) ─────────
    hard_pct = max(0.0, min(100.0, player.armor_hardiness_pct)) / 100.0
    if hard_pct > 0:
        _, worst_mult = cm.worst_damage_multiplier(player.resists, player.resists_pen)
        # Fixed portion (soft damage) can't be armored away.
        soft_dam = enemy.dam * (1.0 - hard_pct)
        soft_after_resist = soft_dam * worst_mult * wrapper
        # Need: (hardened_after_armor + soft_dam) * worst_mult * wrapper <= target_dam
        # hardened_after_armor <= target_dam/(worst_mult*wrapper) - soft_dam
        budget = target_dam / (worst_mult * wrapper) - soft_dam if worst_mult * wrapper > 0 else -1
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
