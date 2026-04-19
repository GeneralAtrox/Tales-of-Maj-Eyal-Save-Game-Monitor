"""
game_data/prodigy_db.py
-----------------------
Parses ToME's prodigy (ubertalent) definitions from the ``tome.team``
archive and exposes a ``T_* → (display_name, primary_stat_key)`` map
used by :func:`gui.memory_reader.MemoryReader.read_prodigies`.

A prodigy's primary stat is the one whose ≥50 base threshold unlocks it.
We infer the stat from which ``data/talents/uber/<stat>.lua`` file the
definition lives in — that's how ToME organises them.

If the archive isn't reachable (non-Steam install, game not running to
extract a temp copy) we fall back to a hardcoded snapshot current as of
ToME 1.7.x. The snapshot is only used when the dynamic parse fails.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Final

from game_data.lua_extractor import (
    RE_NAME,
    RE_NOT_LISTED,
    RE_SHORT_NAME,
    find_tome_team,
    iter_regex_blocks,
    name_to_tid,
)

# Map each prodigy file to the primary stat it gates on.
# Keys match short_name suffixes used in ``ActorStats:defineStat``.
_UBER_STAT_FILES: Final[dict[str, str]] = {
    "data/talents/uber/str.lua": "str",
    "data/talents/uber/dex.lua": "dex",
    "data/talents/uber/const.lua": "con",
    "data/talents/uber/mag.lua": "mag",
    "data/talents/uber/wil.lua": "wil",
    "data/talents/uber/cun.lua": "cun",
}

_PRODIGY_DB: dict[str, tuple[str, str]] | None = None


def get_prodigy_db() -> dict[str, tuple[str, str]]:
    """Return the prodigy DB (cached after first call).

    Mapping: ``T_<ID>`` → ``(display_name, stat_key)`` where ``stat_key`` is
    one of ``str``/``dex``/``con``/``mag``/``wil``/``cun``. All prodigies
    require level 25 and 50+ in the stat.
    """
    global _PRODIGY_DB
    if _PRODIGY_DB is None:
        _PRODIGY_DB = _build_prodigy_db()
    return _PRODIGY_DB


def _build_prodigy_db() -> dict[str, tuple[str, str]]:
    """Return dynamic DB if parseable; otherwise fall back to static snapshot."""
    team_path = find_tome_team()
    if team_path is not None:
        db = _parse_prodigy_archive(team_path)
        if db:
            return db
    return _STATIC_FALLBACK


def _parse_prodigy_archive(team_path: Path) -> dict[str, tuple[str, str]]:
    """Parse prodigy definitions from the game archive, skipping hidden ones."""
    db: dict[str, tuple[str, str]] = {}
    try:
        with zipfile.ZipFile(team_path, "r") as zf:
            names_in_zip = set(zf.namelist())
            for lua_path, stat_key in _UBER_STAT_FILES.items():
                if lua_path not in names_in_zip:
                    continue
                try:
                    src = zf.read(lua_path).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                for block in iter_regex_blocks(src, "uberTalent"):
                    if RE_NOT_LISTED.search(block):
                        continue
                    name_m = RE_NAME.search(block)
                    if not name_m:
                        continue
                    display_name = name_m.group(1)
                    short_m = RE_SHORT_NAME.search(block)
                    if short_m:
                        tid = f"T_{short_m.group(1).upper()}"
                    else:
                        tid = name_to_tid(display_name)
                    db[tid] = (display_name, stat_key)
    except Exception:  # noqa: BLE001
        return {}
    return db


# ── Static fallback snapshot (ToME 1.7.x) ────────────────────────────────────
_STATIC_FALLBACK: Final[dict[str, tuple[str, str]]] = {
    # Constitution
    "T_DRACONIC_BODY": ("Draconic Body", "con"),
    "T_BLOODSPRING": ("Bloodspring", "con"),
    "T_ETERNAL_GUARD": ("Eternal Guard", "con"),
    "T_NEVER_STOP_RUNNING": ("Never Stop Running", "con"),
    "T_ARMOUR_OF_SHADOWS": ("Armour of Shadows", "con"),
    "T_SPINE_OF_THE_WORLD": ("Spine of the World", "con"),
    "T_FUNGAL_BLOOD": ("Fungal Blood", "con"),
    "T_CORRUPTED_SHELL": ("Corrupted Shell", "con"),
    # Cunning  (T_FAST_AS_LIGHTNING excluded: not_listed=true)
    "T_TRICKY_DEFENSES": ("Tricky Defenses", "cun"),
    "T_ENDLESS_WOES": ("Endless Woes", "cun"),
    "T_SECRETS_OF_TELOS": ("Secrets of Telos", "cun"),
    "T_ELEMENTAL_SURGE": ("Elemental Surge", "cun"),
    "T_EYE_OF_THE_TIGER": ("Eye of the Tiger", "cun"),
    "T_WORLDLY_KNOWLEDGE": ("Worldly Knowledge", "cun"),
    "T_ADEPT": ("Adept", "cun"),
    "T_TRICKS_OF_THE_TRADE": ("Tricks of the Trade", "cun"),
    # Dexterity
    "T_FLEXIBLE_COMBAT": ("Flexible Combat", "dex"),
    "T_THROUGH_THE_CROWD": ("Through The Crowd", "dex"),
    "T_SWIFT_HANDS": ("Swift Hands", "dex"),
    "T_WINDBLADE": ("Windblade", "dex"),
    "T_WINDTOUCHED_SPEED": ("Windtouched Speed", "dex"),
    "T_CRAFTY_HANDS": ("Crafty Hands", "dex"),
    "T_ROLL_WITH_IT": ("Roll With It", "dex"),
    "T_VITAL_SHOT": ("Vital Shot", "dex"),
    # Magic  (T_SPECTRAL_SHIELD excluded: not_listed=true)
    "T_ETHEREAL_FORM": ("Ethereal Form", "mag"),
    "T_AETHER_PERMEATION": ("Aether Permeation", "mag"),
    "T_MYSTICAL_CUNNING": ("Mystical Cunning", "mag"),
    "T_ARCANE_MIGHT": ("Arcane Might", "mag"),
    "T_TEMPORAL_FORM": ("Temporal Form", "mag"),
    "T_BLIGHTED_SUMMONING": ("Blighted Summoning", "mag"),
    "T_REVISIONIST_HISTORY": ("Revisionist History", "mag"),
    "T_CAUTERIZE": ("Cauterize", "mag"),
    "T_LICH": ("Lich", "mag"),
    "T_HIGH_THAUMATURGIST": ("High Thaumaturgist", "mag"),
    # Strength
    "T_GIANT_LEAP": ("Giant Leap", "str"),
    "T_TITAN_S_SMASH": ("You Shall Be My Weapon!", "str"),
    "T_MASSIVE_BLOW": ("Massive Blow", "str"),
    "T_STEAMROLLER": ("Steamroller", "str"),
    "T_IRRESISTIBLE_SUN": ("Irresistible Sun", "str"),
    "T_NO_FATIGUE": ("I Can Carry The World!", "str"),
    "T_LEGACY_OF_THE_NALOREN": ("Legacy of the Naloren", "str"),
    "T_SUPERPOWER": ("Superpower", "str"),
    "T_AVATAR_OF_A_DISTANT_SUN": ("Avatar of a Distant Sun", "str"),
    # Willpower
    "T_DRACONIC_WILL": ("Draconic Will", "wil"),
    "T_METEORIC_CRASH": ("Meteoric Crash", "wil"),
    "T_GARKUL_S_REVENGE": ("Garkul's Revenge", "wil"),
    "T_HIDDEN_RESOURCES": ("Hidden Resources", "wil"),
    "T_LUCKY_DAY": ("Lucky Day", "wil"),
    "T_UNBREAKABLE_WILL": ("Unbreakable Will", "wil"),
    "T_SPELL_FEEDBACK": ("Spell Feedback", "wil"),
    "T_MENTAL_TYRANNY": ("Mental Tyranny", "wil"),
    "T_FALLEN": ("Fallen", "wil"),
}
