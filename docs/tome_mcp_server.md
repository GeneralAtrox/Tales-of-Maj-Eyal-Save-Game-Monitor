# ToME Research MCP Server

`tools/tome_mcp_server.py` exposes local ToME/T-Engine research tools over MCP stdio.
It is designed for battle simulator work: find the real Lua combat paths, read exact
source snippets, and inspect current ToME logs without manually grepping archives.

The server reads:

- a cloned or vendored `t-engine4` source tree, if available;
- the installed Steam archives, especially `tome.team` and `te4-1.7.6.teae`;
- `te4_log.txt`, `te4_log_web.txt`, and `debug.log` from the install directory.

The installed archives are the source of truth for the local game build. On this
machine the existing `tools\t-engine4-master` source tree reports `1.7.4`, while
the Steam install reports `1.7.6`; the MCP `source_inventory` tool calls this out.

## Run Directly

From `C:\Users\svjkr\Projects\Codex\TOME_GUI`:

```powershell
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\tome_mcp_server.py --call source_inventory
```

Curated battle simulator research pass:

```powershell
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\tome_mcp_server.py --call battle_simulator_research --args '{"topic":"damage","max_results":40}'
```

Read the melee damage pipeline:

```powershell
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\tome_mcp_server.py --call read_source --args '{"path":"source:game/modules/tome/class/interface/Combat.lua","start_line":380,"line_count":180}'
```

Read the exact installed archive instead of the vendored source:

```powershell
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\tome_mcp_server.py --call read_source --args '{"path":"install:tome.team:mod/class/interface/Combat.lua","start_line":380,"line_count":180}'
```

## MCP Client Config

Use this command for MCP clients that support stdio servers:

```json
{
  "mcpServers": {
    "tome-research": {
      "command": "C:\\Users\\svjkr\\.venvs\\codex\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\svjkr\\Projects\\Codex\\TOME_GUI\\tools\\tome_mcp_server.py",
        "--stdio"
      ],
      "env": {
        "TOME_INSTALL_ROOT": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\TalesMajEyal"
      }
    }
  }
}
```

If you clone the matching upstream source, point the server at it:

```powershell
git clone --depth 1 --branch tome-1.7.6 https://git.net-core.org/tome/t-engine4.git C:\Users\svjkr\Projects\Codex\.tmp\t-engine4-tome-1.7.6
```

Then add:

```json
"TOME_MCP_SOURCE_ROOT": "C:\\Users\\svjkr\\Projects\\Codex\\.tmp\\t-engine4-tome-1.7.6"
```

The server still searches installed archives, so it remains useful even without a
matching cloned tree.

## Tools

- `source_inventory`: paths, archive versions, source versions, logs, and mismatch warnings.
- `search_source`: text or regex search over source, archives, and logs.
- `read_source`: line-numbered source/archive/log reader with path traversal checks.
- `find_lua_definitions`: finds Lua `function`, class, `newTalent`, `newEntity`, and `newEffect` blocks.
- `battle_simulator_research`: curated searches for damage, hit, armor, crit, talent, and practice-runner work.
- `read_game_log`: filtered tail reads for ToME logs.

## Battle Simulator Workflow

Start with `battle_simulator_research` for `overview` or `damage`, then read these anchors:

- `game/modules/tome/class/interface/Combat.lua`: `attackTarget` and `attackTargetWith`.
- `game/engines/default/engine/DamageType.lua`: typed damage projectors.
- `game/engines/default/engine/interface/ActorProject.lua`: projectile, beam, cone, and ball delivery.
- `game/modules/tome/class/Actor.lua`: actor combat fields, temporary values, and resist tables.
- `game/engines/default/engine/interface/ActorTalents.lua`: talent activation and callback flow.
- `game/engines/default/engine/interface/ActorTemporaryEffects.lua`: timed effects and buffs.
- `game/modules/tome/data/damage_types.lua`: ToME-specific damage type behavior.

For simulator fixes, prefer paraphrasing formulas and field flow from GPL source rather
than copying large source blocks into this project.
