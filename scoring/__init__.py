"""Scoring and threat-estimation primitives.

Pure functions and dataclasses — no side effects, no memory reader
imports. Callers (`gui/enemy_panel.py`, upcoming item rating code) pass
explicit values extracted from their own data sources.

Public surface:
    combat_math    — formula primitives (hit rate, armor, resists, crit)
    enemy_threat   — estimate incoming damage as % of player HP
    combat_advice  — inverse: smallest lever to survive an expected hit
"""

from __future__ import annotations

from . import combat_advice, combat_math, enemy_threat

__all__ = ["combat_math", "enemy_threat", "combat_advice"]
