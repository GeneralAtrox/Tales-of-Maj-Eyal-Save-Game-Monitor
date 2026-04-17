# CLAUDE.md

## Project Overview

Tales of Maj'Eyal (ToME) save game monitor. Watches local save files, creates rolling timestamped backups, scrapes character data from the Te4.org vault, and outputs AI-ready JSON character sheets.

## Architecture

```
TOME_SaveMonitor.py        # Entry point → monitor.main()
monitor.py                 # Orchestration: event loop, init, update dispatch
models.py                  # AppConfig, CharacterConfig dataclasses (slots=True)
backups.py                 # Backup creation + retention enforcement
te4_client.py              # Te4.org HTTP API, debounced sync via threading.Timer
parsers.py                 # HTML → JSON transformation (core logic, 800+ lines)
agent.md                   # LLM instructions for character analysis
gui/
  app.py                   # PySide6 GUI entry point
  memory_reader.py         # Live memory reads from t-engine.exe via ReadProcessMemory
  dashboard_tab.py         # Player stats / resource display (sourced from memory)
  enemy_panel.py           # Live enemy list with danger ratings (sourced from memory)
  sprite_composer.py       # Composite sprite rendering from add_mos layers
game_data/
  npc_db.py                # Static NPC/entity database
tools/
  find_lua_state.py        # Dev tool: locate LuaJIT _G in process memory
  probe_entities.py        # Dev tool: dump live entity tables
Tales of Maj'Eyal_v3.CT   # Cheat Engine table (manual use only, not used by code)
```

**Vault data flow**: save change detected → backup created → vault HTML scraped → HTML parsed to JSON → written to `CharacterSheets/data_<folder>.json`

**Live memory data flow**: GUI polls `t-engine.exe` via `ReadProcessMemory` → walks LuaJIT 2.0.2 GCtab structures to find `_G → game → player` and `game → level → entities` → feeds player HP/resources panel and live enemy list with danger ratings.

### What comes from where

| Data | Source |
|---|---|
| Character sheet (stats, talents, equipment, inventory) | Te4.org vault HTML → `parsers.py` → `CharacterSheets/data_*.json` |
| Live HP / mana / stamina / gold | `memory_reader.py` — `game.player` table in process memory |
| Enemies on current level | `memory_reader.py` — `game.level.entities` table in process memory |
| Sprites / composite layers | `memory_reader.py` — `image` field + `add_mos` ordered sub-tables |
| Inventory (Current vs Transmog) | Vault HTML only — transmog items flagged by "This item will automatically be transmogrified when you leave the level." text, bucketed in `parsers.py` |

The `.CT` Cheat Engine table is a separate manual tool for value editing — the Python code does its own independent memory scan and does **not** use or require Cheat Engine.

## Live Memory — Accessible Game Tables

Navigated via `_G → game → ...` using `ReadProcessMemory` on `t-engine.exe` (LuaJIT 2.0.2, 32-bit GCtab traversal).

### Currently read (`memory_reader.py`)

| Path | What we use |
|---|---|
| `game.player` | HP, mana, stamina, vim, psi, positive/negative, hate, paradox, equilibrium, gold, level, exp, rank, image, add_mos sprite layers, combat stats, saves |
| `game.player.stats` | Base stat values (Str/Dex/Con/Mag/Wil/Cun) |
| `game.player.combat` | Mainhand dam/atk/apr/physspeed |
| `game.player.resists` | Per-damage-type resistances |
| `game.player.inc_damage` | Per-damage-type damage bonuses |
| `game.player.resists_pen` | Penetration values |
| `game.player.talents` | Talent levels (T_* keys) |
| `game.player.talents_cd` | Active cooldowns |
| `game.player.has_transmo` | Gates inventory transmog bucket |
| `game.level` | `id` string (zone + level name) |
| `game.level.entities` | All live actors → enemy panel + danger rating |

### Untouched — worth coming back to

| Path | Contents | Priority |
|---|---|---|
| `game.uniques` | Every unique NPC/object/encounter seen or killed (class path → 1.0). ~512 entries. | High — kill tracking, progression |
| `game.visited_zones` | Zone short names → True. ~32 zones. | High — map completion progress |
| `game.party.ingredients` | Alchemy ingredient counts (TROLL_INTESTINE, BEAR_PAW, etc.) | Medium — crafting inventory |
| `game.party.lore_known` | Lore entries discovered (item names, note IDs) | Medium — lore completion |
| `game.state` | Misc world flags: `boss_killed`, `kitty_fed`, `east_orc_patrols`, `has_bearscape`, `unique_death` table | Medium — world state / quests |
| `game.factions` | Tables keyed `angolwen`, `assassin-lair`, etc. — faction standing | Medium — reputation |
| `game.zone` | `name`, `short_name`, `base_level`, `level_range`, `npc_list` (~641), `object_list` (~1537), `trap_list` (~21) | Low — zone meta, object/trap spawns |
| `game.level.data` | `base_level`, `min/max_material_level`, `level_range` for current level | Low — level difficulty info |
| `game.level.map` | Tile/particle/FOV data — complex, likely not practical | Low |
| `game.total_playtime` | Total seconds played (on `game` table directly) | Low — session stats |
| `game.turn` | Current game turn counter | Low — timing |
| `game.target` | `active`, `source_actor`, `target` — what the player is currently targeting | Low — combat overlay |
| `game.player.sustain_talents` | Which sustains are active | Medium — buff tracking |
| `game.player.effects` / `tmp` | Active status effects (buffs/debuffs) | Medium — status display |
| `game.player.talents_cd` | Only one cooldown visible so far — hash part may have more | Medium — full cooldown tracking |

### Not practical
- `_G.__uids` — ~8193 slots, every entity in the game. Too large to scan usefully.
- `game.level.map` — raw tile/particle/lighting data, no clear use case.
- `game.party.members` — appears empty (solo character).

## Setup & Running

```bash
pip install -r requirements.txt   # beautifulsoup4, requests
python TOME_SaveMonitor.py
```

Requires Python 3.14+. ToME saves expected at `~/T-Engine/4.0/tome/save/`. Config auto-created as `config.json` on first run.

## config.json Structure

```json
{
  "save_root": "~/T-Engine/4.0/tome/save",
  "backup_limit": 3,
  "profile_id": "12345",
  "characters": [
    { "folder_name": "char_id", "name": "Display Name", "vault_id": "uuid" }
  ]
}
```

## Outputs

- `Backups/<char_folder>/backup_YYYYMMDD_HHMMSS/` — rolling save backups
- `CharacterSheets/data_<char_folder>.json` — parsed vault data for LLM analysis

## Code Conventions

- **Type hints** everywhere with `from __future__ import annotations`
- **Python 3.14 type aliases**: `type TalentRecord = str | dict[str, TalentFieldValue]`
- **TypedDict** for structured dicts; **dataclasses with `slots=True`** for models
- **Constants** as `Final` uppercase names — 150+ regex patterns in `parsers.py`
- `parsers.py` is pure functions (no state); all side effects live in `monitor.py` / `te4_client.py`
- Errors handled with specific exception types; graceful degradation returns `{}` or `[]`
- Warnings go to `sys.stderr`, not stdout

## Linting

```bash
ruff check .    # line-length 120, rules: B, E, F, I, UP
ruff format .
```

## No Tests

There are no automated tests. Verify changes manually by running the monitor against a real ToME install or by calling parser functions directly on saved HTML fixtures.
