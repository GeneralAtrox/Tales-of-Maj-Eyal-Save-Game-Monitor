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
