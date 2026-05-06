from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from game_data.talent_db import TalentRecord, get_talent_db_by_id


@dataclass(frozen=True, slots=True)
class WeaponTalentMultipliers:
    max_hit: float = 1.0
    burst: float = 1.0
    burst_hits: int = 1


def weapon_multiplier_for_talents(
    talents: Mapping[str, float],
    *,
    db: Mapping[str, TalentRecord] | None = None,
) -> float:
    """Return the largest known weapon multiplier across visible talents."""
    return weapon_multipliers_for_talents(talents, db=db).max_hit


def weapon_multipliers_for_talents(
    talents: Mapping[str, float],
    *,
    db: Mapping[str, TalentRecord] | None = None,
) -> WeaponTalentMultipliers:
    """Return strongest single hit and strongest same-action burst across visible weapon talents."""
    if not talents:
        return WeaponTalentMultipliers()
    records = db if db is not None else get_talent_db_by_id()
    best_hit = 1.0
    best_burst = 1.0
    best_burst_hits = 1
    for raw_id, raw_level in talents.items():
        record = records.get(_normalize_talent_id(raw_id))
        if record is None or not record.npc_usable or record.scaling_family != "weapon" or record.damage_high <= 0.0:
            continue
        hit = _weapon_multiplier(record.damage_low, record.damage_high, raw_level)
        burst_low = record.weapon_burst_low if record.weapon_burst_high > 0.0 else record.damage_low
        burst_high = record.weapon_burst_high if record.weapon_burst_high > 0.0 else record.damage_high
        burst = _weapon_multiplier(burst_low, burst_high, raw_level)
        if hit > best_hit:
            best_hit = hit
        if burst > best_burst:
            best_burst = burst
            best_burst_hits = max(1, record.weapon_burst_hits)
    return WeaponTalentMultipliers(max_hit=best_hit, burst=best_burst, burst_hits=best_burst_hits)


def _weapon_multiplier(low: float, high: float, level: float) -> float:
    level = max(0.0, float(level))
    return low + (high - low) * math.sqrt(level / 5.0)


def _normalize_talent_id(raw_id: str) -> str:
    talent_id = str(raw_id).strip().upper()
    if not talent_id:
        return ""
    if talent_id.startswith("T_"):
        return talent_id
    return f"T_{talent_id}"
