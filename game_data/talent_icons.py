from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
TALENT_ICONS = ROOT / "Icons" / "talents"

# Talent names whose icon filename does not match either the display-name
# snake_case or the authoritative talent id stem.
ICON_NAME_OVERRIDES: dict[str, str] = {
    "Pulverising Auger": "dig.png",
    "Pulverizing Auger": "dig.png",
    "Mirror Image": "mirror_images.png",
    "Temporal Shield": "time_shield.png",
    "Arcane Reconstruction": "heal.png",
    "Ogric Wrath": "ogre_wrath.png",
    "Heavy Armour Training": "armour_training.png",
    "Combat Accuracy": "weapon_combat.png",
    "Dagger Mastery": "knife_mastery.png",
}


def to_display_snake(name: str) -> str:
    s = name.lower().replace("'", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def normalize_icon_name(icon_value: str) -> str:
    icon_value = icon_value.strip()
    if not icon_value:
        return ""
    return PurePosixPath(icon_value).name


def talent_id_stem(talent_id: str) -> str:
    if not talent_id:
        return ""
    return talent_id.removeprefix("T_").lower()


def resolve_talent_icon_path(
    *,
    name: str,
    data_icon: str = "",
    lookup_icon: str = "",
    talent_id: str = "",
) -> Path:
    candidates: list[str] = []

    icon_name = normalize_icon_name(data_icon)
    if icon_name:
        candidates.append(icon_name)

    icon_name = normalize_icon_name(lookup_icon)
    if icon_name:
        candidates.append(icon_name)

    override_name = ICON_NAME_OVERRIDES.get(name)
    if override_name:
        candidates.append(override_name)

    tid_stem = talent_id_stem(talent_id)
    if tid_stem:
        candidates.append(f"{tid_stem}.png")

    candidates.append(f"{to_display_snake(name)}.png")

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = TALENT_ICONS / candidate
        if path.exists():
            return path

    # Return the final deterministic candidate so callers can still display
    # a placeholder while audits detect the missing file explicitly.
    return TALENT_ICONS / candidates[-1]


def audit_unresolved_talent_icons() -> list[tuple[str, str, str]]:
    from game_data.prodigy_db import get_prodigy_db
    from game_data.talent_db import get_talent_db

    missing: list[tuple[str, str, str]] = []
    for name, record in sorted(get_talent_db().items()):
        path = resolve_talent_icon_path(
            name=name,
            data_icon=record.icon,
            lookup_icon=record.icon,
            talent_id=record.talent_id,
        )
        if not path.exists():
            missing.append(("talent", name, path.name))

    for _, record in sorted(get_prodigy_db().items()):
        path = resolve_talent_icon_path(
            name=record.name,
            data_icon=record.icon,
            talent_id=record.talent_id,
        )
        if not path.exists():
            missing.append(("prodigy", record.name, path.name))

    return missing
