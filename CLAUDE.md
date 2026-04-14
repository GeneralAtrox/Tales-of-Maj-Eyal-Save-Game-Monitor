# CLAUDE.md

## Project Overview

Tales of Maj'Eyal (ToME) save game monitor. Watches local save files, creates rolling timestamped backups, scrapes character data from the Te4.org vault, and outputs AI-ready JSON character sheets.

## Architecture

```
TOME_SaveMonitor.py   # Entry point → monitor.main()
monitor.py            # Orchestration: event loop, init, update dispatch
models.py             # AppConfig, CharacterConfig dataclasses (slots=True)
backups.py            # Backup creation + retention enforcement
te4_client.py         # Te4.org HTTP API, debounced sync via threading.Timer
parsers.py            # HTML → JSON transformation (core logic, 800+ lines)
agent.md              # LLM instructions for character analysis
```

**Data flow**: save change detected → backup created → vault HTML scraped → HTML parsed to JSON → written to `CharacterSheets/data_<folder>.json`

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
