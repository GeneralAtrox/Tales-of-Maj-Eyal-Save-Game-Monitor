"""
scoring/ranks.py
----------------
Rank taxonomy and danger scoring for ToME actors.

ToME's ``rank`` field is a numeric value with fractional overloads:

    1    = critter
    2    = normal
    3    = elite
    3.2  = rare
    3.5  = unique
    4    = boss
    5    = elite boss

This module centralises the names, weights, and legacy danger-score
calculation so both the memory reader and UI can share them without
duplicating constants.

The newer damage-based threat model lives in
:mod:`scoring.enemy_threat`; the danger label here is the legacy
rank-relative fallback used when we can't compute real threat (e.g. no
player stats available yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ── Rank constants ───────────────────────────────────────────────────────────

RANK_CRITTER: Final = 1
RANK_NORMAL: Final = 2
RANK_ELITE: Final = 3
RANK_RARE: Final = 3.2
RANK_UNIQUE: Final = 3.5
RANK_BOSS: Final = 4
RANK_ELITE_BOSS: Final = 5

RANK_NAMES: Final[dict[int, str]] = {
    1: "Critter",
    2: "Normal",
    3: "Elite",
    4: "Boss",
    5: "Elite Boss",
}


def rank_label(rank: float | None) -> str:
    """Convert a numeric rank to a human-readable label.

    The fractional rank tiers (3.2 = rare, 3.5 = unique) sit between elite
    and boss, so we must check them *before* truncating to int — otherwise
    ``int(3.5) == 3`` matches :data:`RANK_NAMES` first and every rare/unique
    gets mislabelled "Elite".
    """
    if rank is None:
        return "Unknown"
    if rank >= 5:
        return "Elite Boss"
    if rank >= 4:
        return "Boss"
    if rank >= 3.5:
        return "Unique"
    if rank >= 3.2:
        return "Rare"
    if rank >= 3:
        return "Elite"
    if rank >= 2:
        return "Normal"
    if rank >= 1:
        return "Critter"
    return f"Rank {rank:.1f}"


# ── Danger labels ────────────────────────────────────────────────────────────

DANGER_TRIVIAL: Final = "Trivial"
DANGER_EASY: Final = "Easy"
DANGER_MODERATE: Final = "Moderate"
DANGER_DANGEROUS: Final = "Dangerous"
DANGER_DEADLY: Final = "Deadly"


# Rank → weight for danger calculation (higher = scarier)
_RANK_WEIGHT: Final[dict[int, float]] = {
    1: 0.2,  # critter
    2: 1.0,  # normal
    3: 1.8,  # elite
    4: 3.0,  # boss
    5: 4.0,  # elite boss
}


def rank_weight(rank: float) -> float:
    """Return a danger multiplier for ``rank``. Fractional ranks step up."""
    r = int(rank)
    w = _RANK_WEIGHT.get(r, 1.0)
    if rank >= 3.5:
        return max(w, 2.4)  # unique
    if rank >= 3.2:
        return max(w, 2.0)  # rare
    return w


# ── Legacy danger model (minimal types so we don't depend on memory_reader) ─


@dataclass(slots=True)
class _PlayerDangerInputs:
    """Minimal slice of player stats used by :func:`compute_danger`."""

    level: float
    max_life: float
    armor: float
    defense: float
    phys_save: float
    spell_save: float
    mental_save: float


@dataclass(slots=True)
class _EnemyDangerInputs:
    """Minimal slice of enemy fields used by :func:`compute_danger`."""

    rank: float
    level: float
    max_life: float
    armor: float
    defense: float
    phys_save: float
    spell_save: float
    mental_save: float


def compute_danger(
    enemy: _EnemyDangerInputs,
    player: _PlayerDangerInputs | None,
) -> tuple[str, float]:
    """Compute a danger label and numeric score for an enemy relative to the player.

    Returns ``(label, score)`` — higher score = more dangerous. If player
    stats are unavailable, falls back to a rank-only assessment.

    The score is a rough heuristic — the damage-based
    :mod:`scoring.enemy_threat` model is preferred when we have the data.
    """
    if player is None or player.level <= 0:
        score = rank_weight(enemy.rank) * 10 + enemy.level
        if score > 40:
            return DANGER_DEADLY, score
        if score > 25:
            return DANGER_DANGEROUS, score
        if score > 15:
            return DANGER_MODERATE, score
        if score > 8:
            return DANGER_EASY, score
        return DANGER_TRIVIAL, score

    rw = rank_weight(enemy.rank)

    # Level delta — normalised so +5 levels ≈ +1.0
    level_factor = (enemy.level - player.level) / 5.0

    # HP ratio — cap contribution at 5× to avoid runaway
    hp_ratio = (enemy.max_life / player.max_life) if player.max_life > 0 else 1.0
    hp_factor = min(hp_ratio, 5.0) / 2.0

    # Save / defense deltas
    enemy_avg_save = (enemy.phys_save + enemy.spell_save + enemy.mental_save) / 3.0
    player_avg_save = (player.phys_save + player.spell_save + player.mental_save) / 3.0
    save_delta = (enemy_avg_save - player_avg_save) / 15.0
    def_delta = ((enemy.armor + enemy.defense) - (player.armor + player.defense)) / 20.0

    raw = rw * (1.0 + 0.4 * level_factor + 0.2 * hp_factor + 0.1 * save_delta + 0.1 * def_delta)
    score = max(0.0, raw)

    if score >= 3.5:
        return DANGER_DEADLY, score
    if score >= 2.2:
        return DANGER_DANGEROUS, score
    if score >= 1.3:
        return DANGER_MODERATE, score
    if score >= 0.7:
        return DANGER_EASY, score
    return DANGER_TRIVIAL, score
