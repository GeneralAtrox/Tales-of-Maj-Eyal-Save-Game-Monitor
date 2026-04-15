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
