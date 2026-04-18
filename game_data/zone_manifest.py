from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZoneEntry:
    display_name: str
    short_name: str | None
    tier: int
    floors: int = 1
    notes: str = ""
    boss: str | None = None
    race_req: str | None = None
    optional: bool = False


ZONES: list[ZoneEntry] = [
    # ── Tier 1 — Levels 1–10 ──────────────────────────────────────────────
    ZoneEntry("Trollmire", "trollmire", 1, floors=2, boss="Bill the Stone Troll"),
    ZoneEntry(
        "Norgos' Lair",
        "norgos-lair",
        1,
        floors=1,
        boss="Norgos, the Frozen",
        optional=True,
        notes="Spawns after clearing Trollmire.",
    ),
    ZoneEntry("Ruins of Kor'Pul", "ruins-kor-pul", 1, floors=2, boss="The Shade"),
    ZoneEntry("Scintillating Caves", "scintillating-caves", 1, floors=3, boss="Borfast the Broken"),
    ZoneEntry(
        "Rhaloren Camp",
        "rhaloren-camp",
        1,
        floors=1,
        boss="Rhaloren Inquisitor",
        optional=True,
        notes="Shaloren quest chain only.",
    ),
    ZoneEntry("Heart of the Gloom", "heart-gloom", 1, floors=1, boss="The Withering Thing"),
    ZoneEntry("The Deep Bellow", "deep-bellow", 1, floors=2, race_req="Dwarf", notes="Dwarf starting zone."),
    # ── Tier 2 — Levels 10–20 ─────────────────────────────────────────────
    ZoneEntry(
        "The Maze",
        "maze",
        2,
        floors=7,
        boss="Minotaur of the Labyrinth",
        notes="Find the Minotaur on the bottom floor.",
    ),
    ZoneEntry(
        "Sandworm Lair",
        "sandworm-lair",
        2,
        floors=4,
        boss="Sandworm Queen",
        notes="Bottom floor leads to Ancient Elven Ruins.",
    ),
    ZoneEntry("Daikara", "daikara", 2, floors=5, notes="Avoid the Temporal Rift — high spike damage."),
    ZoneEntry("Old Forest", "old-forest", 2, floors=4, notes="Do not descend to Lake of Nur until Tier 3."),
    ZoneEntry("Thieves' Tunnels", "thieves-tunnels", 2, floors=1, notes="Under the city of Derth."),
    ZoneEntry(
        "Lumberjack Village",
        "lumberjack-village",
        2,
        floors=1,
        optional=True,
        notes="Spawns from a random quest just north of Last Hope.",
    ),
    ZoneEntry("Ruined Halfling Complex", "halflings-ruins", 2, floors=3),
    ZoneEntry("Unknown Tunnels", "unknown-tunnels", 2, floors=3, notes="Hostile faction tunnels."),
    # ── Tier 3 — Levels 20–30 ─────────────────────────────────────────────
    ZoneEntry(
        "Dreadfell",
        "dreadfell",
        3,
        floors=10,
        boss="Grand Corruptor",
        notes="Main story zone. Long — bring consumables.",
    ),
    ZoneEntry("Lake of Nur", "lake-nur", 3, floors=4, notes="Accessed from bottom of Old Forest."),
    ZoneEntry(
        "Ancient Elven Ruins", "ancient-elven-ruins", 3, floors=5, notes="Accessed from bottom of Sandworm Lair."
    ),
    ZoneEntry("Abashed Expanse", "abashed-expanse", 3, floors=3, notes="East side. High-level encounters."),
    ZoneEntry(
        "Mark of the Spellblaze",
        "mark-spellblaze",
        3,
        floors=1,
        boss="Spellblaze Crystal",
        notes="Triggered by world event.",
    ),
    ZoneEntry("Temporal Rift", "temporal-rift", 3, floors=1, notes="Side area off Daikara — skip on first visit."),
    # ── Tier 4 — Levels 30–40 ─────────────────────────────────────────────
    ZoneEntry("Vor Armoury", "vor-armoury", 4, floors=7, notes="East. High physical and fire damage."),
    ZoneEntry("Rak'Shor Pride", "rak-shor-pride", 4, floors=5, notes="Blight and darkness heavy."),
    ZoneEntry("Grushnak Pride", "grushnak-pride", 4, floors=5, notes="Physical heavy."),
    ZoneEntry("Gorbat Pride", "gorbat-pride", 4, floors=5),
    ZoneEntry("Vor Pride", "vor-pride", 4, floors=5),
    ZoneEntry("Reknor", "reknor", 4, floors=5, race_req="Dwarf"),
    # ── Tier 5 — Levels 40–50 ─────────────────────────────────────────────
    ZoneEntry("Last Hope Graveyard", "last-hope-graveyard", 5, floors=2, notes="Late-game unlock."),
    ZoneEntry("High Peak", "high-peak", 5, floors=5, boss="Argoniel", notes="Final zone. Clear all four prides first."),
]
