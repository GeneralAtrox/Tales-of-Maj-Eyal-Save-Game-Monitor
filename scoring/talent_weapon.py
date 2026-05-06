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
    cooldowns: Mapping[str, float] | None = None,
    range_to_target: float | None = None,
) -> float:
    """Return the largest known weapon multiplier across visible talents."""
    return weapon_multipliers_for_talents(
        talents,
        db=db,
        cooldowns=cooldowns,
        range_to_target=range_to_target,
    ).max_hit


def weapon_multipliers_for_talents(
    talents: Mapping[str, float],
    *,
    db: Mapping[str, TalentRecord] | None = None,
    cooldowns: Mapping[str, float] | None = None,
    resources: Mapping[str, float] | None = None,
    range_to_target: float | None = None,
) -> WeaponTalentMultipliers:
    """Return strongest single hit and strongest same-action burst across visible weapon talents."""
    if not talents:
        return WeaponTalentMultipliers()
    records = db if db is not None else get_talent_db_by_id()
    talents_by_id = _normalize_number_map(talents)
    cooldowns_by_id = _normalize_number_map(cooldowns) if cooldowns is not None else {}
    best_hit = 1.0
    best_burst = 1.0
    best_burst_hits = 1
    for talent_id, raw_level in talents_by_id.items():
        record = records.get(talent_id)
        if (
            record is None
            or not record.npc_usable
            or record.mode == "passive"
            or record.scaling_family != "weapon"
            or record.damage_high <= 0.0
        ):
            continue
        if cooldowns_by_id.get(talent_id, 0.0) > 0.0:
            continue
        if resources is not None and not _resource_costs_available(record.resource_costs, resources):
            continue
        if not _target_range_available(record, range_to_target):
            continue
        aux_level = talents_by_id.get(record.weapon_aux_talent_id, 0.0) if record.weapon_aux_talent_id else 0.0
        hit = _weapon_multiplier(record.damage_low, record.damage_high, raw_level, aux_level)
        burst_low = record.weapon_burst_low if record.weapon_burst_high > 0.0 else record.damage_low
        burst_high = record.weapon_burst_high if record.weapon_burst_high > 0.0 else record.damage_high
        burst = _weapon_multiplier(burst_low, burst_high, raw_level, aux_level)
        if hit > best_hit:
            best_hit = hit
        if burst > best_burst:
            best_burst = burst
            best_burst_hits = max(1, record.weapon_burst_hits)
    return WeaponTalentMultipliers(max_hit=best_hit, burst=best_burst, burst_hits=best_burst_hits)


def _weapon_multiplier(low: float, high: float, level: float, aux_level: float = 0.0) -> float:
    level = max(0.0, float(level))
    level += max(0.0, float(aux_level)) / 2.0
    return low + (high - low) * math.sqrt(level / 5.0)


def _normalize_talent_id(raw_id: str) -> str:
    talent_id = str(raw_id).strip().upper()
    if not talent_id:
        return ""
    if talent_id.startswith("T_"):
        return talent_id
    return f"T_{talent_id}"


def _normalize_number_map(values: Mapping[str, float] | None) -> dict[str, float]:
    if not values:
        return {}
    return {_normalize_talent_id(key): float(value) for key, value in values.items()}


def _target_range_available(record: TalentRecord, range_to_target: float | None) -> bool:
    if range_to_target is None or not record.requires_target or record.target_range is None:
        return True
    limit = max(0.0, record.target_range + max(0.0, record.target_radius))
    return range_to_target <= limit


def _resource_costs_available(costs: Mapping[str, float], resources: Mapping[str, float]) -> bool:
    for resource, cost in costs.items():
        if cost > 0 and resources.get(resource, 0.0) < cost:
            return False
    return True
