# ToME Game Data — Structure Reference

All paths relative to:
`C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal\game\modules\tome.team\data\`

---

## 1. Talent Descriptions — `data/talents/`

### File layout
~90 `.lua` files in subdirectories by school:
`cunning/`, `cursed/`, `corruptions/`, `celestial/`, `spells/`, `techniques/`, etc.

Each file defines multiple talents using `newTalent{ ... }`.

### Key fields per talent

| Field       | Type     | Notes |
|-------------|----------|-------|
| `name`      | string   | Display name |
| `type`      | array    | `{"school/tree", tier}` |
| `points`    | int      | Max talent level |
| `mode`      | string   | `"passive"` / `"sustained"` / absent (active) |
| `cooldown`  | int      | Turns |
| `mana`, `stamina`, `psi`, … | int | Resource cost |
| `range`     | int/fn   | Targeting range |
| `require`   | table ref | Stat prerequisites, e.g. `cuns_req1` |
| `getDamage` | function | Called as `t.getDamage(self, t)` |
| `getDuration` | function | Called as `t.getDuration(self, t)` |
| `info`      | **function** | Returns the description string |
| `action`    | function | Active use logic |

### `info` field — the description source

`info` is always a Lua function, never a plain string:
```lua
info = function(self, t)
    local damage   = t.getDamage(self, t)
    local duration = t.getDuration(self, t)
    return ([[Description text with %d%% damage and %d turns.]]):tformat(damage, duration)
end,
```

The actual human-readable template lives between `([[` and `]])`.
Format codes used:
- `%d`    — integer
- `%0.2f` — float with 2 decimal places
- `%%`    — literal `%`
- Arguments are Lua expressions using helper functions (`t.getDamage(self,t)`, `damDesc(...)`, local file functions)

### Parsing strategy

To extract a readable template without executing Lua:

1. Regex-extract text between `([[` and `]])` in each `info` function body
2. Replace `%d`, `%0.2f` etc. with `{N}` placeholders
3. Note the argument expressions after `:tformat(` for context (they name the stat involved)
4. Local helper functions (e.g. `gloomTalentsMindpower`) are defined at the top of each file before the talent blocks — they can be extracted too

**Example extracted template:**
```
You make a low blow dealing {1}% unarmed damage.
The target's physical save is reduced by {2} and
immunities to 50% for {3} turns.
```

With argument labels: `{1}=damage%, {2}=power, {3}=duration`

### Parsing challenges
- `info` requires Lua execution for exact runtime values (scale with level, stats)
- Helper functions differ per file — no shared stdlib
- `require` fields reference global constants (`cuns_req1`, `cuns_req2`, …) defined either at the file top or in `engine/` base files

### Talent Tree Layout And Ordering

The in-game talent tree layout is not driven by the raw `data/talents/...` files alone.
The runtime UI uses the actor's category list and each category's ordered talent list.

Confirmed source files inside:
`C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal\game\modules\tome.team`

- `mod/dialogs/LevelupDialog.lua`
- `mod/dialogs/CharacterSheet.lua`
- `mod/class/interface/PlayerDumpJSON.lua`

Confirmed runtime rules:

1. Category order is built from `ipairs(self.actor.talents_types_def)`.
2. Talent order inside a category is built from `ipairs(tt.talents)`.
3. Hidden categories are skipped with `not tt.hide`.
4. A category is shown only if `self.talents_types[tt.type]` is not `nil`.
5. In the levelup dialog, known categories are shown before locked-but-available categories.
6. Category labels are formatted as:
   `("%s / %s"):format(_t(cat, "talent category"):capitalize(), tt.name:capitalize())`

Practical implication for this project:

- Do not derive final talent ordering from unordered memory hash iteration.
- Do not hardcode talent category order per class.
- Prefer the actor/runtime category list semantics used by the Lua UI.
- If reproducing the in-game ordering from live memory, mirror the same rules the dialogs use:
  category list order from runtime defs, then known categories first, then `tt.talents` order within each category.

---

## 2. Quests — `data/quests/`

~30 `.lua` files, one quest per file.

### Structure
Quests are not data objects — each file is executed as a Lua program:
```lua
name = _t"Quest Name"
desc = function(self, who)
    if self:isCompleted("stage_id") then
        return _t"Completed desc."
    end
    return _t"Active desc."
end
on_grant   = function(self) ... end
on_complete = function(self) ... end
```

### Key fields

| Field       | Notes |
|-------------|-------|
| `name`      | `_t"..."` translated string — easy to extract |
| `desc`      | Function — dynamic, depends on completion state |
| `id`        | Sometimes runtime-computed (`"escort-duty-"..zone.."-"..level`) |
| Event hooks | `on_grant`, `on_complete`, `on_status_change` — full Lua |

### Parsing notes
- `name` can be extracted statically with `_t"([^"]+)"` regex
- `desc` cannot be rendered without running Lua — skip dynamic rendering, display raw template text only
- Stage IDs are string literals passed to `self:isStatus("stage_id")` — can be regex-extracted

---

## 3. Name Tables — `data/languages/names/`

Plain `.txt` files, one name per line. No Lua.

| File pattern       | Contents |
|--------------------|----------|
| `{race}_male.txt`  | Male names for that race |
| `{race}_female.txt`| Female names |
| `demon.txt`        | Demon names (unsexed) |
| `sources/`         | Markov-chain source material (same format) |

Races covered: `cornac`, `dwarf`, `elf`, `halfling`, `ogre`, `shalore`, `thalore`, `yeek`, `undead`, etc.

**Parsing**: `names = open(path).read().splitlines()` — trivial.

---

## 4. Race & Class Data — `data/birth/`

### Files
```
birth/
  classes/   — one .lua per class (Mage, Rogue, Warrior, …)
  races/     — one .lua per race (Cornac, Dwarf, Elf, …)
  descriptors.lua  — top-level descriptor definitions
  worlds.lua       — campaign/world options
  sexes.lua        — sex options
```

### Lua pattern
```lua
newBirthDescriptor{
    type = "race",           -- "race", "class", or "subclass"
    name = "Dwarf",
    desc = {
        _t"First paragraph of lore.",
        _t"Second paragraph.",
    },
    stats = { str=3, con=3, dex=-1 },
    talents = {
        [ActorTalents.T_MINER]         = 1,
        [ActorTalents.T_GEM_MAGIC]     = 1,
    },
    talents_types = {
        ["technique/combat-training"] = {true, 0.3},  -- {unlocked, mastery}
    },
    copy = {
        -- Arbitrary actor fields merged at character creation
        -- Contains resolvers.* calls (function values) for equipment/inscriptions
        life_rating = 12,
        ...
    },
    power_source = {technique=true},
}
```

### Key fields

| Field           | Notes |
|-----------------|-------|
| `name`          | Display name — string |
| `desc`          | Array of `_t"..."` strings — strip `_t` wrapper to get text |
| `stats`         | `{str, mag, dex, wil, cun, con}` deltas |
| `talents`       | Starting talent levels, keyed by `ActorTalents.T_NAME` |
| `talents_types` | School unlocks + mastery values |
| `power_source`  | `{technique=true}` / `{arcane=true}` / `{nature=true}` |

### Parsing strategy
- `name` — plain string
- `desc` — regex `_t"([^"]+)"` or `_t\[\[([^\]]*)\]\]` to extract paragraphs
- `stats` — regex each `{key=N, ...}` key-value pair
- `talents` — extract `T_NAME` constant names and integer levels
- `copy` block — skip resolver calls, only extract plain numeric fields

---

## 5. Damage Types — `data/damage_types.lua`

This file is **engine logic**, not a data table. Structure:
- First ~200 lines: one giant `setDefaultProjector(function(...) end)` defining shared modifier logic (crits, difficulty scaling, stun penalties, immunities)
- Individual type definitions appear later via `DamageType:add{ name="FIRE", ... }`

**Not suitable for static parsing.** Damage type names (`FIRE`, `COLD`, `MIND`, etc.) can be extracted as strings from the `DamageType:add{name="..."}` pattern, but the actual damage formulas require executing the engine.

Use the names only as display labels (already known from player sheet data).

---

## 6. NPCs & Enemies — `data/general/npcs/`

~30 `.lua` files, one per creature family:
`animals.lua`, `undead.lua`, `demons.lua`, `humanoids.lua`, `horror.lua`, etc.

### Two-step inheritance pattern
```lua
-- 1. Base template (shared stats, no name/sprite)
newEntity{
    define_as = "BASE_NPC_TROLL",
    type = "humanoid", subtype = "troll",
    stats = { str=25, con=15, dex=8, mag=3, wil=10, cun=8 },
    ...
}

-- 2. Concrete variant (inherits base, adds name/desc/sprite/talents)
newEntity{
    base = "BASE_NPC_TROLL",
    name = "mountain troll",
    desc = _t[[A large brutal creature.]],
    image = "npc/troll_m.png",    -- single-tile sprite
    rank = 2,
    level_range = {10, 20},
    ...
}
```

### Sprite path patterns

**Single-tile** (most common):
```lua
image = "npc/troll_m.png"
```

**Multi-tile / overlaid** (tall or composite sprites):
```lua
resolvers.nice_tile{
    image = "invis.png",          -- placeholder base tile
    add_mos = {{
        image   = "npc/undead_horror_necrotic_abomination.png",
        display_h = 2,            -- 2 tiles tall
        display_y = -1,           -- shift up 1 tile
    }}
}
```
→ The real sprite path is `add_mos[1].image`.

**Regex to extract NPC sprite path from either pattern:**
```python
import re

# Direct image field
m = re.search(r'\bimage\s*=\s*"(npc/[^"]+\.png)"', entity_text)

# Multi-tile add_mos
m = re.search(r'add_mos\s*=\s*\{\{[^}]*image\s*=\s*"(npc/[^"]+\.png)"', entity_text)
```

### Key fields per NPC

| Field          | Notes |
|----------------|-------|
| `name`         | Display name (string or `_t"..."`) |
| `desc`         | `_t[[...]]` multiline string — static lore text |
| `image`        | Direct sprite path `"npc/<file>.png"` |
| `resolvers.nice_tile` | Multi-tile sprite, real path in `add_mos[1].image` |
| `type`/`subtype` | Taxonomy strings |
| `rank`         | 1=critter, 2=normal, 3=elite, 4=boss, 5=elite boss |
| `level_range`  | `{min, max}` |
| `rarity`       | Spawn weight integer |
| `combat`       | Attack/damage stats table |
| `stats`        | Base stat table |
| `resists`      | Damage resistance table |
| `resolvers.talents` | Talent loadout: `{[T.T_NAME]={base=N, every=M, max=P}}` |

### Parsing strategy for descriptions
```python
import re

def extract_npc_desc(entity_text: str) -> str:
    m = re.search(r'desc\s*=\s*_t\[\[([^\]]*)\]\]', entity_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'desc\s*=\s*_t"([^"]+)"', entity_text)
    if m:
        return m.group(1)
    return ""
```

### Matching NPC names to sprites already in `Icons/npc/`
The `image` field value (e.g. `"npc/troll_m.png"`) maps directly to `Icons/npc/troll_m.png`.
The existing `_find_icon()` fuzzy-match in `gui/enemy_panel.py` already handles
entity display names → icon filenames.  Parsing NPC files gives us the ground-truth
mapping: `name → "npc/<file>.png"`, which can replace the fuzzy match entirely.

---

## Implementation Priority

| Data source     | Value                             | Difficulty | Priority |
|-----------------|-----------------------------------|------------|----------|
| NPC desc+sprite | Show enemy lore + confirm sprites | Low (regex)| **High** |
| Race/Class desc | Enrich character sheet header     | Low (regex)| High |
| Talent info     | Show talent description on click  | Medium (template extraction) | High |
| Names           | Context/flavour                   | Trivial    | Low |
| Quests          | Active quest tracking             | Hard (Lua) | Low |
| Damage types    | Engine logic only                 | Very hard  | Skip |

---

## Talent Description Rendering Plan

Since exact values require Lua execution, use a **two-tier approach**:

1. **Template tier** (static, always available): Extract text between `([[` and `]])`, replace format codes with labelled placeholders from argument names. Show as tooltip/description.

2. **Live tier** (future): If we ever embed a Lua interpreter (e.g. `lupa`/`luajit` via ctypes), call the actual `info(self, t)` function with the player's current stats for exact values.

For now implement Tier 1 only.

### Regex for template extraction
```python
import re

def extract_talent_template(info_source: str) -> str:
    """
    Extract the description template from a Lua info function body.
    info_source is the raw Lua source of the info function.
    """
    m = re.search(r'\(\[\[(.*?)\]\]\)', info_source, re.DOTALL)
    if not m:
        return ""
    text = m.group(1).strip()
    # Replace Lua format codes with readable placeholders
    text = re.sub(r'%0\.\d+f', '{#}', text)
    text = re.sub(r'%d', '{#}', text)
    text = text.replace('%%', '%')
    return text
```
