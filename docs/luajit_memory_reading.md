# LuaJIT Memory Reading — Verified Reference

Reading live game state from `t-engine.exe` (Tales of Maj'Eyal) via
Python `ctypes` + `ReadProcessMemory`. No external dependencies.

**Confirmed version**: LuaJIT 2.0.2, 32-bit (x86), no GC64.

---

## GC Header gct Values vs TValue itype Values

These are **different** and the distinction is critical:

- **GC header `gct`** — stored as the plain type index (0, 1, 2, ...)
- **TValue `itype`** — stored as `~index` (bitwise NOT: 0xFFFFFFFF, 0xFFFFFFFE, ...)

The `gct` byte lives at offset +5 of any GC object (GCstr, GCtab, lua_State).
The `itype` lives at bytes 4–7 (upper 32 bits) of an 8-byte NaN-boxed TValue.

| Type       | GC header gct | TValue itype | Notes                    |
|------------|---------------|--------------|--------------------------|
| nil        | —             | 0xFFFFFFFF   | No GC object             |
| false      | —             | 0xFFFFFFFE   | No GC object             |
| true       | —             | 0xFFFFFFFD   | No GC object             |
| GCstr      | **4** (0x04)  | 0xFFFFFFFB   | String                   |
| GCtab      | **11** (0x0B) | 0xFFFFFFF4   | Table                    |
| lua_State  | **6** (0x06)  | 0xFFFFFFF9   | Thread / coroutine       |
| number     | —             | < 0xFFFFFFF2 | Entire 8 bytes = double  |

**Why this matters**: scanning for GCtab objects means searching for byte
`0x0B` at offset +5 (NOT `0xF4`). We wasted hours scanning for `0xF4`.

---

## Struct Layouts (32-bit, verified)

These are not process addresses. They are LuaJIT 2.0.2 32-bit struct
offsets, and they stay reusable only while the embedded LuaJIT build remains
32-bit/no-GC64 with the same object layout. The source reference is
`tools\t-engine4-master\src\luajit2\src\lj_obj.h`; `lj_arch.h` confirms
`LJ_32=1`, `LJ_64=0` for the 32-bit target.

### TValue — 8 bytes (NaN-boxed)

```
bytes 0–3:  lo    GCRef (pointer) or low bits of IEEE 754 double
bytes 4–7:  it    type tag (uint32_t)
```

If `it < 0xFFFFFFF2`, all 8 bytes are a `double` (number).
Otherwise `lo` is a 32-bit pointer to a GC object and `it` identifies the type.

### GCstr — 16 + len bytes

```
+0   GCRef nextgc    (4)
+4   uint8 marked    (1)
+5   uint8 gct       (1)    = 0x04
+6   uint8 reserved  (1)
+7   uint8 unused    (1)
+8   uint32 hash     (4)
+12  uint32 len      (4)    string length in bytes
+16  char data[]            null-terminated string
```

### GCtab — 32 bytes

```
+0   GCRef nextgc    (4)
+4   uint8 marked    (1)
+5   uint8 gct       (1)    = 0x0B
+6   uint8 nomm      (1)
+7   int8  colo      (1)
+8   MRef  array     (4)    ptr to TValue[] array part
+12  GCRef gclist    (4)
+16  GCRef metatable (4)
+20  MRef  node      (4)    ptr to Node[] hash part
+24  uint32 asize    (4)    array part size
+28  uint32 hmask    (4)    hash mask (node count = hmask + 1)
```

### Node (hash table entry) — 24 bytes

```
+0   TValue val      (8)    the value
+8   TValue key      (8)    the key
+16  MRef   next     (4)    collision chain (0 = end)
+20  MRef   freetop  (4)    only meaningful in node[0]
```

String keys always live in the hash part, not the array part.

---

## Finding _G (the Global Table)

We do NOT search for `lua_State*` — too many false positives. Instead we
scan directly for GCtab objects that contain the string key `"game"`.

### Algorithm

1. Scan all readable committed memory pages (via `VirtualQueryEx`)
2. For each 4-byte-aligned offset, check if byte at +5 == `0x0B` (GCtab)
3. Read `node` (+20) and `hmask` (+28) from the local buffer (no extra reads)
4. Filter: `hmask >= 63` (large table like `_G`), `node` is a heap address
5. For surviving candidates, do a single bulk `ReadProcessMemory` of the
   entire node array, then check for key `"game"` by comparing GCstr data

**Performance**: Phase 1 (local scan) takes ~6 seconds for ~1 GB of committed
memory. Phase 2 (string key checks) takes ~2 seconds for ~6000 candidates.

### Re-attachment

The `_G` address changes when:
- The game process restarts (new PID)
- A new character is loaded (ToME may create a new Lua VM)

The GUI auto-detects stale addresses: if `read_player_hp()` returns `None`
for 5 consecutive 1-second polls, the reader detaches and re-attaches on the
next game-poll cycle (~3 seconds). Total recovery: ~10 seconds.

---

## Runtime Singleton Anchor

**Best anchor for the GUI:** discover `_G`, then cache `_G.game` as the
runtime singleton.

`_G` is the safest discovery root because it is recognizable from table
shape plus well-known global keys. `game` is the fastest recurring root
because almost every live read hangs from it:

```
_G -> game
game -> player
game -> level
game -> zone
game -> state
game -> visited_zones
```

Do not persist the numeric `game` address across process launches. It is a
Lua heap table, not a module symbol. Reuse it only inside the same process
after validating that it still looks like a `GCtab` and still exposes expected
ToME singleton fields such as `player`, `level`, `zone`, `state`,
`visited_zones`, `party`, or `turn`.

`MemoryReader` validates `game` when attaching or rediscovering it, then uses
a fast cached pointer for hot polling. It revalidates periodically and forces
rediscovery immediately if a dependent chain such as `game.player` disappears.
`GCtab` validation also checks that each table header address is inside a
committed, readable `VirtualQueryEx` region before reading it.

### Address Classification

The following live values were validated on 2026-05-05 against
`C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal\t-engine.exe`,
PID `10340`. These are examples only; they are **current-session only**.

| Address | Meaning | Region | Reusable? | Rebase rule |
|---|---|---|---|---|
| `0x0CDD1328` | `_G` GCtab | `MEM_PRIVATE`, read/write | Same PID + creation time only, after validation | Rediscover by GCtab scan; do not rebase |
| `0x369B94E0` | `game` GCtab | `MEM_PRIVATE`, read/write | Same Lua VM only, after validation | Resolve from `_G.game`; do not rebase |
| `0x369B8250` | `game.player` GCtab | `MEM_PRIVATE`, read/write | Same loaded character only, after validation | Resolve from `game.player`; do not rebase |
| `0x3730DBB0` | `game.level` GCtab | `MEM_PRIVATE`, read/write | Current level only, after validation | Resolve from `game.level`; do not rebase |
| `0x3730DDC8` | `game.level.entities` GCtab | `MEM_PRIVATE`, read/write | Current level only, after validation | Resolve from `game.level.entities`; do not rebase |

The executable image was loaded at `0x00400000` with image size `0x4DD000`
in that session. Module addresses are the only values that can be expressed
as `Module.Base + RVA`. Lua heap objects must always be rediscovered by
walking the validated table chain.

### Cheat Engine Table Note

`Tales of Maj'Eyal_v3.CT` uses:

```
aobscanmodule(tome_23,t-engine.exe,83 79 04 FF 74 36)
```

That byte pattern was unique in the current `t-engine.exe` file and maps to
RVA `0x92060` (`Module.Base + 0x92060` -> `0x00492060` in the observed
process). It is rebasable as an AOB/RVA hook point, but it patches executable
code and is therefore not used by the Python GUI. Treat the CT symbols
(`tome_13`, `tome_17`, etc.) as Cheat Engine runtime scratch addresses, not
portable app offsets.

### Rebase Validator

Use the read-only validator after ToME updates:

```text
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\validate_memory_rebase.py
```

When ToME is running, the validator also prints live Lua roots with their
`VirtualQueryEx` region classification. `_G`, `game`, `game.player`,
`game.level`, and `game.level.entities` should report `OK` in committed,
readable memory and should also be confirmed as `GCtab`. They are still
**not** rebasable; the classification only proves the current-session pointer
is safe to read.

For a pass/fail update gate against the checked-in baseline:

```text
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\validate_memory_rebase.py --strict
```

The baseline lives at `docs\memory_rebase_baseline.json`. Refresh it only
after manually confirming a new ToME executable still has valid anchors:

```text
C:\Users\svjkr\.venvs\codex\Scripts\python.exe tools\validate_memory_rebase.py --write-baseline
```

The validator also parses `Tales of Maj'Eyal_v3.CT` directly. Every
`aobscanmodule(...)` row is checked for uniqueness, mapped to an RVA, and
live-verified when ToME is running. Registered CT symbols are classified so
runtime scratch labels are not mistaken for portable offsets.

Current executable fingerprint from the local Steam install:

| Field | Value |
|---|---|
| Path | `C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal\t-engine.exe` |
| Size | `0x4BBA00` (`4962816` bytes) |
| SHA-256 | `d6da7808503366ebb2a412f56c647b36e9f8aee85687ee672818851a0f67964d` |
| PE image base | `0x00400000` |
| PE image size | `0x4DD000` |

Current static anchors:

| Anchor | Result | Rebase value | Policy |
|---|---|---|---|
| `83 79 04 FF 74 36 0F B6 46 FD 8B 29 8B 49 04 89` | unique | `.text` RVA `0x92060` | CT hook context only |
| `83 79 04 FF 74 36` | unique | `.text` RVA `0x92060` | exact CT AOB |
| `LuaJIT 2.0.2` bytes | unique | `.rdata` RVA `0x375793` | runtime-layout fingerprint |

Current CT symbol classification:

| Symbol | Rebasable? | Policy |
|---|---:|---|
| `tome_23` | yes | `aobscanmodule` hook, reusable as `Module.Base + 0x92060` after AOB validation |
| `tome_7`, `tome_9`, `tome_12`, `tome_13`, `tome_15`, `tome_17` | no | Auto Assembler labels inside the injected allocation; runtime scratch only |

The baseline also records the reader's LuaJIT layout assumptions: `TValue`
size `8`, `Node` size `24`, `GCtab` size `32`, key field offsets, and the
`LJ_T*` NaN-box tags. If those assumptions change in code or in the embedded
runtime, `--strict` reports `luajit_layout` drift.

After an update:

1. Re-run `tools\validate_memory_rebase.py`.
2. If the LuaJIT version string changes or disappears, do not trust the
   `GCtab`, `GCstr`, `Node`, or `TValue` offsets until they are re-verified.
3. If an AOB has zero matches, the hook moved or changed; rediscover it before
   using the CT table.
4. If an AOB has multiple matches, treat it as ambiguous; widen the signature
   with more surrounding bytes before using it.
5. If an AOB remains unique but its RVA changes, the anchor is still
   rebasable. Use the new `Module.Base + RVA`.
6. Never rebase Lua heap roots. Rediscover `_G`, then resolve `game`,
   `game.player`, `game.level`, and child tables through Lua table lookups.

---

## Walking Lua Tables

```
_G  →  hash_find(_G, "game")       →  GCtab* game
game →  hash_find(game, "player")  →  GCtab* player
player → hash_find(player, "life") →  TValue (double)
```

The hash lookup reads all nodes in one bulk read per table, then iterates
locally. Each node's key is checked: `key.it == 0xFFFFFFFB` (string),
then `GCstr.len` and `GCstr.data` are compared.

---

## Verified Player Fields

All read as doubles from `game.player`:

| Key            | Description          |
|----------------|----------------------|
| `life`         | Current HP           |
| `max_life`     | Maximum HP           |
| `mana`         | Current mana         |
| `max_mana`     | Maximum mana         |
| `stamina`      | Current stamina      |
| `max_stamina`  | Maximum stamina      |
| `vim`          | Current vim          |
| `max_vim`      | Maximum vim          |
| `money`        | Gold                 |

Resource keys follow the pattern `<resource>` / `max_<resource>`.

---

## Implementation

- **`gui/memory_reader.py`** — `MemoryReader` class
  - `attach()` — finds PID, opens process, scans for `_G`
  - `detach()` — closes handle, clears cached addresses
  - `read_player_hp()` → `(life, max_life) | None`
  - `read_player_resources()` → `dict[str, float]`
- **`gui/dashboard_tab.py`** — wires `MemoryReader` into the dashboard
  - Background thread for initial attach (~6 s scan)
  - 1-second QTimer polls HP, updates header label
  - Color-coded: green (>50%), yellow (25–50%), red (<25%)
  - Auto re-attach after 5 consecutive read failures
- **`gui/app.py`** — UAC auto-elevation at startup
  - Checks `IsUserAnAdmin()`, relaunches via `ShellExecuteW("runas")`
  - Falls back gracefully if UAC declined (no HP, everything else works)

---

## No External Dependencies

Uses only `ctypes` + `ctypes.wintypes` (stdlib). The original plan to use
`pymem` was dropped because pip had network issues and `ctypes` works fine.

---

## Sources

- LuaJIT 2.0.2 `src/lj_obj.h` — struct definitions, type tag constants
- DarkGod blog (2013): confirms ToME uses LuaJIT 2
- Memory scan of t-engine.exe: confirmed `LuaJIT 2.0.2` version string
- GCstr diagnostic: confirmed gct=0x04 at +5, len at +12, data at +16
