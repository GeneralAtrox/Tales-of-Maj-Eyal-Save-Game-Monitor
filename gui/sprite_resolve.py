"""
gui/sprite_resolve.py
---------------------
Pure helpers for filtering and picking actor sprite images.

The memory-walking part (``add_mos`` sub-tables in the ``game.player``
actor entry) stays in :mod:`gui.memory_reader` — this module owns only
the "which image path is a usable character sprite" judgement so the
logic stays testable without touching the game process.

ToME's image fields carry four distinct things:

* ``image = "npc/troll_f.png"`` — a direct, usable sprite.
* ``image = "invis.png"`` — placeholder; the real layers live in ``add_mos``.
* ``add_mos[i].image`` — ordered composite layers (shadows and the actor
  itself interleaved).
* ``attachement_spots`` — random bosses occasionally stash the sprite path
  here when nothing else is set.

We reject anything outside ``npc/``/``player/``, reject ``invis.png``, and
reject layers whose name contains ``shadow`` (those are lighting passes,
not the actor).
"""

from __future__ import annotations

from typing import Final

_IMAGE_PREFIXES: Final = ("npc/", "player/")


def is_usable_sprite(image: str) -> bool:
    """Return True when ``image`` is a playable actor sprite path.

    Filters out the engine's invisible placeholder, shadow-only layers,
    and any non-actor tile path.
    """
    if not image:
        return False
    if image == "invis.png":
        return False
    if "shadow" in image:
        return False
    return any(image.startswith(p) for p in _IMAGE_PREFIXES)


def pick_actor_image(
    raw_image: str,
    sprite_layers: list[str],
    base_layer_image: str,
    attachement_spots: str,
) -> str:
    """Pick the single best representative sprite for an actor.

    Preference order: direct ``image`` field (if usable) → a layer marked
    ``is_inate = "base"`` → the first usable ``add_mos`` layer →
    ``attachement_spots`` as a last resort.
    """
    if is_usable_sprite(raw_image):
        return raw_image
    if is_usable_sprite(base_layer_image):
        return base_layer_image
    if sprite_layers:
        return sprite_layers[0]
    if is_usable_sprite(attachement_spots):
        return attachement_spots
    return ""
