# ToME Source Review — Live Memory Schemas & App Gap Analysis

Notes taken from reviewing the open-source ToME / T-Engine 4 code at
`tools/t-engine4-master/`. Complements the existing `tome_game_data.md`
(static file parsing) and `luajit_memory_reading.md` (low-level struct
layouts). This doc focuses on **what Lua tables actually look like at
runtime** and **what the app currently misses**.

Source: `tools/t-engine4-master/` — GPLv3, for reference only. Do not
copy substantial code into this project; paraphrase field layouts.

---

## 1. Live memory field map (confirmed from source)

### `game` (global game singleton)
`game/modules/tome/class/Game.lua`

Top-level fields the app has reason to read:

| Field | Shape | App reads? | Notes |
|---|---|---|---|
| `player` | table (Actor) | ✅ | Main character |
| `level` | table (Level) | ✅ | Current level |
| `zone` | table (Zone) | ✅ (partial) | Current zone |
| `state` | table | ✅ (`unique_death` only) | World flags incl. `boss_killed`, `kitty_fed`, `east_orc_patrols` |
| `visited_zones` | `{short_name=true}` hash | ✅ | Map progress |
| `uniques` | `{class_path=1.0}` hash | ❌ | Every unique seen/killed, ~512 entries |
| `party` | table | ❌ | `party.ingredients` (alchemy), `party.lore_known` |
| `factions` | `{faction_id={standing=N}}` | ❌ | Reputation |
| `turn` / `total_playtime` | number | ❌ | Session timing |
| `target` | `{active, source_actor, target}` | ❌ | Current combat target |

### `Actor` / `game.player`
`game/engines/default/engine/Actor.lua` +
`game/modules/tome/class/Actor.lua` + mixed-in interfaces.

**Direct numeric fields initialised in `Actor:init()`** (lines 156–250 of the ToME Actor.lua) — all confirmed present in memory for a live player:

```
life, max_life, die_at
combat_def, combat_armor, combat_armor_hardiness
combat_atk, combat_apr, combat_dam
combat_physcrit, combat_physspeed, combat_spellspeed, combat_mindspeed
combat_spellcrit, combat_spellpower, combat_mindcrit, combat_mindpower
combat_physresist, combat_spellresist, combat_mentalresist
global_speed, global_speed_base, global_speed_add, movement_speed
fatigue, spell_cooldown_reduction
unused_stats, unused_talents, unused_generics, unused_talents_types, unused_prodigies
healing_factor, sight, lite, size_category, rank
life_rating, mana_rating, vim_rating, stamina_rating, positive_negative_rating, psi_rating
x, y, level, exp, money
```

The app already reads most of the combat-relevant ones.

**Subtables (hash part)** on the player:

| Key | Shape | App reads? | Notes |
|---|---|---|---|
| `stats` | **int-keyed** `{1..7: int}` | ⚠️ (see bug below) | STR=1, DEX=2, MAG=3, WIL=4, CUN=5, CON=6, LCK=7 |
| `inc_stats` | int-keyed same as stats | ❌ | Stat bonuses from gear/talents |
| `resists` | `{damage_type_name: percent}` | ✅ | Keys are upper-case strings (FIRE, COLD, …) |
| `resists_cap` | `{type_or_"all": cap}` | ❌ | Max resist ceiling (default `{all=100}`) |
| `resists_pen` | `{type: percent}` | ✅ | Resistance penetration |
| `resists_self` | `{type: percent}` | ❌ | Self-damage resist |
| `inc_damage` | `{type: percent}` | ✅ | Per-type damage bonuses |
| `inc_damage_actor_type` | `{type: percent}` | ❌ | Bonus vs specific creature types |
| `damage_affinity` | `{type: percent}` | ❌ | Absorb-as-heal percent |
| `flat_damage_cap`, `flat_damage_armor` | tables | ❌ | Caps / armour per type |
| `talents` | `{T_ID: level}` | ✅ | Talent levels learned |
| `talents_cd` | `{T_ID: turns}` | ✅ | Active cooldowns |
| `talents_auto` | `{T_ID: mode}` | ❌ | Auto-use settings |
| `sustain_talents` | `{T_ID: true_or_params}` | ❌ | **Active sustains — big miss** |
| `talents_types` | `{type_key: true/false}` | ✅ | Known talent categories |
| `talents_types_mastery` | `{type_key: float}` | ✅ | Category mastery values |
| `tmp` | `{EFF_ID: {dur, effect_id, power, src, …}}` | ❌ | **Temporary effects — big miss** |
| `inven` | `{int_id: {worn, name, short_name, stack_limit, max, <items>}}` | ✅ | Bucket-of-items per slot |
| `quests` | `{quest_id: {state, …}}` | ❌ | Quest progress |
| `all_kills`, `all_kills_kind` | tables | ❌ | Kill tracker |
| `esp`, `esp_range` | table / number | ❌ | Telepathy |
| `last_learnt_talents` | ordered table | ❌ | Recent levelup picks |
| `descriptor` | `{class, subclass, race, subrace, sex, world}` | ❌ | Birth info |
| `has_transmo` | number/bool | ✅ | Transmog chest unlocked |
| `exp` / `level` | numbers | ✅ | XP and character level |
| `add_mos` | ordered list of `{image, display_h, display_y, …}` | ✅ | Sprite layers |

### `game.level` — `game/engines/default/engine/Level.lua`

```
entities       = {uid_number: actor_table}   -- hash keyed by entity uid
entities_list  = {type_name: list}            -- category lookup (NPC types, objects)
perm_entities_list = same but persistent
e_array        = array                        -- insertion / turn order
data           = zone-specific runtime data
tmpdata        = transient (cleared on reload)
level, map, id, short_name, sublevels, sublevel_id
```

Confirmed behaviour in `_M:addEntity`:
`self.entities[e.uid] = e` — keys are numeric uids, not dense 1..N.

**Implication**: the app's `_tab_iter_table_values(h, entities_tab)` is
correct (iterates hash part values). But keys are 64-bit ints stored as
doubles; we don't currently use the uid, only the value pointer.

### `game.zone` — `game/modules/tome/class/Zone.lua`

```
short_name, name, base_level, max_level
level_range = {min, max}
npc_list, object_list, trap_list
events
```

The app reads `short_name` and `max_level`; the rest is unused.

### Effects — `game/engines/default/engine/interface/ActorTemporaryEffects.lua`

`self.tmp` table on every actor:

```lua
self.tmp[eff_id] = {
    dur        = <turns remaining>,
    effect_id  = "EFF_XYZ",
    power      = <magnitude>,      -- effect-specific
    src        = <source actor>,   -- table pointer, could be self
    param1..N  = <...>,
    __orig_params = {…},           -- snapshot for reapplication
}
```

Keys are uppercase `EFF_<NAME>`. Definitions live in
`game/modules/tome/data/timed_effects/{physical,mental,magical,other,floor}.lua`
under `newEffect{}` blocks with fields `name`, `desc`, `type`,
`status` ("beneficial" or "detrimental"), `decrease` (default 1).

**App does not read this.** Reading `player.tmp` would immediately give
a live buff/debuff list with durations.

### Inventory — `game/engines/default/engine/interface/ActorInventory.lua`

`self.inven` is keyed by **integer slot id** (not string). Each bucket:

```lua
self.inven[slot_id] = {
    worn        = <bool, is this an equipped slot>,
    id          = <slot_id>,
    name        = "MAINHAND",    -- uppercase short name
    short_name  = "mainhand",
    stack_limit = N,
    max         = <capacity>,
    [1], [2], …                   -- the actual Object tables
}
```

Slot names in ToME body table (from `data/birth/descriptors.lua`):
`INVEN, QS_MAINHAND, QS_OFFHAND, MAINHAND, OFFHAND, FINGER (×2), NECK,
LITE, BODY, HEAD, CLOAK, HANDS, BELT, FEET, TOOL, QUIVER, QS_QUIVER`.

`QS_` prefix = quickswap weapon set. The app currently groups these
under Mainhand/Offhand — probably fine, but noting they exist.

### Resources — `game/modules/tome/data/resources.lua`

Confirmed defined resources (all accessible as direct fields
`self.<name>` and `self.max_<name>`):

```
life, air, stamina, mana, equilibrium, vim,
positive, negative, hate, paradox, psi, soul, feedback
```

App reads: life, mana, stamina, vim, positive, negative, psi, hate,
paradox, equilibrium, money. **Missing: `air`, `soul`, `feedback`**.
(`air` is breath — only relevant underwater. `soul` is Necromancer.
`feedback` is Solipsist.)

### Stats — `game/modules/tome/load.lua` lines 183–190

```
STAT_STR = 1   (str)
STAT_DEX = 2   (dex)
STAT_MAG = 3   (mag)
STAT_WIL = 4   (wil)
STAT_CUN = 5   (cun)
STAT_CON = 6   (con)
STAT_LCK = 7   (lck)
```

`self.stats[id]` after init uses **integer keys**. String keys like
`self.stats.str` only exist transiently during old-save migration
(ActorStats:init lines 59–75).

---

## 2. Bugs & inaccuracies found in current code

### 🔴 `read_prodigies()` looks up stats by string key

`gui/memory_reader.py:1747-1749`:

```python
for stat in ("str", "dex", "con", "mag", "wil", "cun"):
    v = _tab_get_number(h, stats_tab, stat)
    player_stats[stat] = v if v is not None else 0.0
```

`player.stats` is int-keyed after init. These lookups return `None`
for every stat on a normally-loaded character → `player_stats[stat]`
is always `0.0` → the `>= 50.0` gate never passes → prodigy list is
always empty past the level-25 gate.

**Fix**: read the `stats` subtable's array part by integer index:

```python
# stats[1]=STR, stats[2]=DEX, stats[3]=MAG, stats[4]=WIL, stats[5]=CUN, stats[6]=CON
_STAT_INDEX_BY_NAME = {"str": 1, "dex": 2, "mag": 3, "wil": 4, "cun": 5, "con": 6}
```

Then use a new helper that reads TValue at `array_ptr + (i-1)*8`
(similar to `_tab_array_get_table` but returns a double).

Alternative: read `inc_stats` too and sum — that matches `getStat()`
semantics. Prodigy availability actually uses base stat + inc_stats,
i.e. the *displayed* value. Worth matching that.

### 🟡 `read_player_stats()` → `_dump_numeric_subtable("stats")` would also be empty

Not called today, but if someone tries to dump stats via `_tab_dump_flat`
they'll get nothing because it only scans string keys. Same fix as above.

### 🟡 `_tab_dump_all` subtable prefixing misses int-keyed tables

`_tab_dump_flat` only returns string-keyed entries. When called on
`stats` (int-keyed), result is `{}`. `all_fields["stats.str"]` etc. in
`EntityInfo.all_fields` is never populated for entities. Not a crash,
just dead data — currently hidden behind the debug tooltip.

### 🟡 `read_player_exp()` uses a hard-coded XP curve

`memory_reader.py:1585`: `needed = 90 * (2 * next_level - 1)`.
ToME's actual curve is defined by `engine/interface/ActorLevel.lua` —
check there before trusting the value. Alternative: read `exp_table`
if it's exposed, otherwise read `max_exp` if the engine caches it.

### 🟡 `read_visited_zones` and `read_unique_deaths` are copy-pasted

Both walk the hash part looking for string key → `LJ_TTRUE` value pairs.
~40 lines of duplicated node-walking. Extract:

```python
def _tab_string_keys_with_true(h, tab_ptr) -> set[str]: ...
```

---

## 3. Untapped data that would meaningfully improve the app

Ranked by payoff-per-effort:

### Tier A — small code change, big UX

1. **Active effects (`player.tmp`)** — show buffs/debuffs with duration.
   Read: hash of `EFF_*` strings → table with `dur`, `effect_id`,
   `power`. Present in dashboard as a buff strip.

2. **Active sustains (`player.sustain_talents`)** — show which
   sustains are up. Simple boolean hash.

3. **Fix prodigy stats lookup** — ~15 line change (above).

4. **Consolidate prodigy parsing + NPC parsing** — both hit
   `tome.team` with regex. One `game_data/lua_extractor.py` module
   with `extract_blocks(zf, path, opener="newEntity")` would dedupe.

### Tier B — medium effort, genuine value

5. **Kill tracker (`game.uniques`)** — 512-entry hash mapping class
   paths to 1.0 for anything ever seen. Combined with the existing
   `unique_death` set, gives "uniques encountered vs defeated" stats.

6. **Current-level map progress** — `game.visited_zones` is already
   read; add zone completion % by cross-ref with the static zone list
   from `data/zones/*/zone.lua` (one-time scan).

7. **Talent descriptions from source** — already planned per the
   existing `tome_game_data.md`. The static regex approach handles
   templates; live values require running Lua.

8. **Effect definitions lookup** — parse `data/timed_effects/*.lua`
   once (like npc_db) to map `EFF_NAME → {desc, type, status}`.
   Enables pretty rendering of the buff strip from step 1.

### Tier C — interesting but expensive

9. **`game.party.ingredients`** — alchemy inventory. Easy read but
   narrow audience (mostly alchemists).

10. **Factions (`game.factions`)** — per-faction standing. Fine to
    read but not often consulted outside roleplay.

---

## 4. Simplification opportunities in `memory_reader.py`

The file is 1839 lines — some of that is load-bearing LuaJIT traversal
and shouldn't change. But several things can shrink:

1. **Dedupe hash iteration** — extract `_tab_string_keys_with_true`,
   `_tab_iter_nodes` (yield key_bytes, val_it, val_lo for each string-keyed
   entry). Replaces the hand-rolled loops in `_tab_dump_flat`,
   `read_visited_zones`, `read_unique_deaths`, and the prodigy talent
   scan. ~80 lines of savings.

2. **Move prodigy DB** out to `game_data/prodigy_db.py` alongside
   `npc_db.py`. The parsing logic (zip → regex → dict) is duplicated
   anyway.

3. **Rank constants** — `RANK_*` + `RANK_NAMES` + `_RANK_WEIGHT` +
   `_rank_label` + `compute_danger` could live in a small
   `scoring/ranks.py` (there's already a `scoring/` folder).

4. **`_IMAGE_PREFIXES` / sprite resolution** — the 4-branch image
   resolution (`_extract_actor_sprite`) is used by both player sprite
   and entity sprites. Pure function, move to `gui/sprite_resolve.py`.

5. **Attach cache** — currently tied to module-level file path. Could
   live in an `AttachCache` dataclass with explicit load/save, easier
   to test.

6. **`_EntitySubtables` hard-coded list** — could come from a central
   schema dict (e.g. the table map from section 1 above). Keeps
   field-name drift from creeping in.

---

## 5. Performance — realistic wins

1. **Skip `_tab_dump_all` per-entity when unused.** `read_entities`
   calls `_tab_dump_all` on every actor (full hash walk + 5 subtable
   walks). For a level with 30 actors and 100-node hash parts that's
   18,000 string compares per poll. Only a few fields
   (`name`, `life`, `rank`, `x`, `y`, `faction`, `image`) are used by
   the enemy panel; dump the rest lazily when a row is expanded.

2. **Cache `game.player` pointer across calls within the same tick.**
   Every `read_*` call re-walks `_G → game → player`. One resolve per
   poll cycle is enough; invalidate on detach or when life read fails.

3. **Bulk-read the hash node array once per dump.** `_tab_dump_flat`
   already does a single bulk read of the whole node array — good.
   But the per-key `_rpm(h, gcs+12, 4)` for string length is still a
   syscall per string key. Amortise by reading all GCstr headers in
   one pass after collecting candidates, sorted by address.

4. **Drop the player-scan step in `read_entities` when `_player_table`
   is cached.** Currently it refreshes `player_tab` just to exclude
   self from the iteration, even if `_player_table` is set.

5. **Attach scan is ~6s.** If we cache the _G address keyed by
   `(pid, process_creation_key)` (already done ✓) and validate on
   reattach, re-runs are cheap. The 6s hit only happens on first
   launch per game session — acceptable.

6. **Effect / sustain reads would add minimal cost** — each is a
   single subtable hash walk, ~same cost as `resists`.

---

## 6. Accuracy — what the app could get right but doesn't

1. **Prodigy availability** — broken (section 2).

2. **Damage type id keys vs string keys.** The Actor's `resists`
   table is actually keyed by the `DamageType` enum ID in some places
   and by uppercase name in others depending on when it was written.
   Safer: parse `data/damage_types.lua` for the id→name map on
   startup and normalise on read.

3. **Resistance cap** — app displays raw `resists[TYPE]` but the cap
   (`resists_cap`) can exceed 100 for specific types. `effectiveResist
   = min(resists[TYPE], resists_cap[TYPE] or resists_cap.all)`.

4. **Armor hardiness affects physical damage cap**, not flat
   mitigation. Current `compute_danger` mixes armor + defense
   arithmetically, which matches ToME's rough feel but not the actual
   hit-calc. A more faithful model: use the exposed `Combat.lua`
   `attackTargetWith` formula shape (still approximate, no RNG).

5. **`level_range` vs `level`**. Enemies have both a spawn
   `level_range` (zone data) and a concrete `level` (actor field).
   Danger should weight against the enemy's actual `level`, which the
   app already does. Fine.

6. **Rank fractional values** confirmed as used by ToME (rare=3.2,
   unique=3.5). The `_rank_label` logic is correct — good.

---

## 7. Suggested first round of changes

If we only pick the high-value / low-risk items:

1. Fix prodigy stats lookup (read int-keyed `stats`).  [bug, ~20 LOC]
2. Extract shared `_tab_iter_true_keys` helper; dedupe visited_zones,
   unique_deaths, and the prodigy talent scan.  [cleanup, ~60 LOC saved]
3. Add `read_player_effects()` reading `player.tmp` — returns
   `list[{name, dur, power, source}]`. Wire into dashboard.  [new feature]
4. Add `read_sustain_talents()` — returns talent-id set. Display
   alongside effects.  [new feature]
5. Move prodigy parsing + NPC parsing to a shared
   `game_data/lua_extractor.py`.  [cleanup]

Each of these is independently shippable and doesn't touch the load-
bearing LuaJIT traversal code.
