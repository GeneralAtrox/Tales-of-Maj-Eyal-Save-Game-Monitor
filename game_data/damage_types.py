from __future__ import annotations

from typing import Final

BASE_DAMAGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "ACID",
        "ARCANE",
        "BLIGHT",
        "COLD",
        "DARKNESS",
        "FIRE",
        "LIGHT",
        "LIGHTNING",
        "MIND",
        "NATURE",
        "PHYSICAL",
        "STEAM",
        "TEMPORAL",
    }
)

_DAMAGE_TYPE_ALIASES: Final[dict[str, str]] = {
    "BLEED": "PHYSICAL",
    "BOUNCE_SLIME": "NATURE",
    "CORRUPTED_BLOOD": "BLIGHT",
    "DRAINLIFE": "BLIGHT",
    "GOLEM_FIREBURN": "FIRE",
    "ITEM_ACID_CORRODE": "ACID",
    "ITEM_LIGHTNING_DAZE": "LIGHTNING",
    "ITEM_MIND_EXPOSE": "MIND",
    "LITE": "LIGHT",
    "LITE_LIGHT": "LIGHT",
    "MUCUS": "NATURE",
    "PESTILENT_BLIGHT": "BLIGHT",
    "POISON": "NATURE",
    "RANDOM_POISON": "NATURE",
    "SANGUINE": "BLIGHT",
    "SLIME": "NATURE",
    "WARP": "TEMPORAL",
    "WORMBLIGHT": "BLIGHT",
}


def normalize_damage_type(damage_type: str | None, default: str = "PHYSICAL") -> str:
    """Return the canonical base damage key for one-base ToME damage types.

    Mixed or unknown damage types are preserved so callers can display them
    without pretending they are a single base resist.
    """
    if damage_type is None:
        return default
    key = str(damage_type).strip()
    if not key:
        return default
    if key.lower() == "all":
        return "all"
    key = key.upper()
    if key in BASE_DAMAGE_TYPES:
        return key
    if key in _DAMAGE_TYPE_ALIASES:
        return _DAMAGE_TYPE_ALIASES[key]
    if key == "ICE" or key.startswith("ICE_") or key.startswith("COLD") or key == "MINDFREEZE":
        return "COLD"
    if key.startswith("FIRE"):
        return "FIRE"
    if key.startswith("LIGHTNING"):
        return "LIGHTNING"
    if key.startswith("ACID"):
        return "ACID"
    if key.startswith("MIND"):
        return "MIND"
    if key.startswith("PHYS"):
        return "PHYSICAL"
    if key.startswith("BLIGHT"):
        return "BLIGHT"
    if key.startswith("DARK"):
        return "DARKNESS"
    if key.startswith("LIGHT"):
        return "LIGHT"
    if key.startswith("TEMPORAL"):
        return "TEMPORAL"
    if key.startswith("NATURE"):
        return "NATURE"
    if key.startswith("ARCANE"):
        return "ARCANE"
    return key
