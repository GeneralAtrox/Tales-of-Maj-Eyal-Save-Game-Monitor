from __future__ import annotations

import math
from collections.abc import Mapping

from game_data.talent_db import TalentRecord, get_talent_db_by_id


def weapon_multiplier_for_talents(
    talents: Mapping[str, float],
    *,
    db: Mapping[str, TalentRecord] | None = None,
) -> float:
    """Return the largest known weapon multiplier across visible talents."""
    if not talents:
        return 1.0
    records = db if db is not None else get_talent_db_by_id()
    best = 1.0
    for raw_id, raw_level in talents.items():
        record = records.get(_normalize_talent_id(raw_id))
        if record is None or record.scaling_family != "weapon" or record.damage_high <= 0.0:
            continue
        best = max(best, _weapon_multiplier(record, raw_level))
    return best


def _weapon_multiplier(record: TalentRecord, level: float) -> float:
    level = max(0.0, float(level))
    return record.damage_low + (record.damage_high - record.damage_low) * math.sqrt(level / 5.0)


def _normalize_talent_id(raw_id: str) -> str:
    talent_id = str(raw_id).strip().upper()
    if not talent_id:
        return ""
    if talent_id.startswith("T_"):
        return talent_id
    return f"T_{talent_id}"
