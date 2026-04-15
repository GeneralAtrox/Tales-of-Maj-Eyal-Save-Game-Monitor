# Enemy Scanning — Implementation Plan

Read enemies from `game.level.entities` via the same LuaJIT memory reader.

---

## Data Path

```
_G → game → level → entities → [each actor table]
```

`game.level.entities` is a Lua table mapping entity UIDs to actor objects.
Each actor has the same structure as `game.player` (they inherit from
`mod.class.Actor`).

---

## Scan Trigger: Map Change Detection

**Do NOT poll entities every second.** The entity list only changes when
the player moves to a new map. Detect this by monitoring:

```
game.level.id        — unique string per level instance
game.level.level     — numeric depth
```

Poll `game.level.id` every 1–2 seconds (one string read, very cheap).
When it changes → trigger a full entity scan. This avoids hundreds of
ReadProcessMemory calls per second.

---

## Verified Entity Table Structure (2026-04-16)

Probed from `ruined-dungeon-1` level:

- `game.level.entities` is a GCtab with `asize=0`, `hmask=31`
- **Keys are numeric UIDs** (e.g. 4477.0, 4585.0) stored in the hash part
- Each value is a GCtab (actor table)
- `rank` returns **None as a string** — likely stored as a **number**:
  - 1 = critter, 2 = normal, 3 = elite, 3.2 = rare, 3.5 = unique,
    4 = boss, 5 = elite boss (ToME rank values)
- `dead` is a boolean — check TValue itype == 0xFFFFFFFD (LJ_TTRUE)
- Named enemies like "Jaedemas the Guardian" (faction=fearscape)
  and "Ffovur the wretchling" (faction=enemies) appear in the list

## Entity Scan

When a new map is detected:

1. Read `game.level.entities` (GCtab)
2. Iterate all entries in hash part (asize=0 so no array part)
3. For each entry whose value is a GCtab (an actor):
   - Read `rank` as a **number** — skip if rank <= 1 (critter)
   - Skip if `dead` == true
   - Read identifying fields:

### Fields to Read Per Enemy

| Field         | Type    | Description                          |
|---------------|---------|--------------------------------------|
| `name`        | string  | Display name                         |
| `life`        | number  | Current HP                           |
| `max_life`    | number  | Maximum HP                           |
| `rank`        | string  | critter/normal/elite/rare/unique/boss|
| `level`       | number  | Actor level                          |
| `faction`     | string  | Faction name (enemies-of-player etc) |
| `x`, `y`      | number  | Map position                         |

### Offensive stats (from `combat` sub-table or flat fields)

| Field                  | Description           |
|------------------------|-----------------------|
| `combat_atk`           | Attack power          |
| `combat_dam`           | Physical damage       |
| `combat_spellpower`    | Spellpower            |
| `combat_mindpower`     | Mindpower             |

### Defensive stats

| Field                  | Description           |
|------------------------|-----------------------|
| `combat_armor`         | Armor value           |
| `combat_armor_hardiness`| Armor hardiness      |
| `combat_def`           | Defense               |
| `combat_physresist`    | Physical save         |
| `combat_spellresist`   | Spell save            |
| `combat_mentalresist`  | Mental save           |
| `resists`              | Table of damage type resistances |

---

## Rank Filter

Only display actors with rank **above critter**:

```python
INTERESTING_RANKS = {"normal", "elite", "rare", "unique", "boss", "elite boss"}
```

Sort display by rank priority (boss first, then rare, etc).

---

## Performance Budget

- **Map change poll**: 1 string read every 1–2 s → negligible
- **Full entity scan**: ~100–300 actors × ~10 fields × ReadProcessMemory
  = ~1000–3000 reads. At ~1 μs per read = ~3 ms total. Well within budget.
- **Re-scan only on map change** — not every second

After initial scan, enemy HP could optionally be polled every 2–3 s for
live updates during combat, but only for the filtered set (typically <20
interesting enemies per map).

---

## GUI Display

Add an "Enemies" section or panel (location TBD — could be a new sub-tab
in Characters, or a collapsible section in the dashboard).

For each interesting enemy, show:
- **Name** (bold if rare/unique/boss)
- **HP bar** — life / max_life with color coding
- **Level** and **Rank** badge
- **Faction**
- **Key stats** — armor, saves, resistances (collapsed by default)

---

## Implementation Steps

1. Add `read_level_id()` to `MemoryReader` — returns `game.level.id` string
2. Add `read_entities()` to `MemoryReader` — returns list of entity dicts
3. Add level-change detection timer in dashboard (1–2 s poll)
4. Build enemy display widget
5. Wire scan trigger to map change detection

---

## Known (Resolved)

- **Entity table structure** — VERIFIED: numeric UID keys in hash part,
  `asize=0`, each value is a GCtab actor.
- **Dead actors** — check `dead` field: TValue itype 0xFFFFFFFD = true.
- **Player in entities** — filter by comparing GCtab pointer to
  `game.player` address.
- **Rank field** — stored as number, not string. Need to read as double.
- **Resistances table** — `resists` is a nested table; deferred for now.
