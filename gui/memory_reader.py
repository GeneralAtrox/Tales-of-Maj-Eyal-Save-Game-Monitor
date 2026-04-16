"""
memory_reader.py
----------------
Reads live game state from t-engine.exe (LuaJIT 2.0.2, 32-bit) via
ReadProcessMemory.  Finds the Lua global table (_G) on first attach,
then polls game.player.life / max_life every tick.

Usage from the GUI:
    reader = MemoryReader()
    reader.attach()                   # find t-engine.exe + _G
    hp = reader.read_player_hp()      # (life, max_life) or None
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import subprocess

# ── Win32 constants ───────────────────────────────────────────────────────────
PROCESS_VM_READ           = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT                = 0x1000
PAGE_NOACCESS             = 0x01
PAGE_GUARD                = 0x100

_k32 = ctypes.windll.kernel32


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_void_p),
        ("AllocationBase",    ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize",        ctypes.c_size_t),
        ("State",             ctypes.wintypes.DWORD),
        ("Protect",           ctypes.wintypes.DWORD),
        ("Type",              ctypes.wintypes.DWORD),
    ]


# ── LuaJIT 2.0.2 constants (32-bit, no GC64) ────────────────────────────────
#
# GC header gct values:  GCstr=4, GCtab=11 (0x0B), lua_State=6
# TValue itype values:   LJ_TSTR=0xFFFFFFFB, LJ_TTAB=0xFFFFFFF4
#                         number: itype < 0xFFFFFFF2

_GCT_TAB   = 0x0B
_LJ_TSTR   = 0xFFFFFFFB
_LJ_TTAB   = 0xFFFFFFF4
_LJ_TNUMX  = 0xFFFFFFF2
_NODE_SIZE = 24


# ── Low-level memory access ──────────────────────────────────────────────────

def _rpm(h: int, addr: int, n: int) -> bytes | None:
    buf  = ctypes.create_string_buffer(n)
    read = ctypes.c_size_t(0)
    ok   = _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(read))
    return bytes(buf) if (ok and read.value == n) else None


def _ru32(h: int, addr: int) -> int | None:
    b = _rpm(h, addr, 4)
    return struct.unpack('<I', b)[0] if b else None


def _rf64(h: int, addr: int) -> float | None:
    b = _rpm(h, addr, 8)
    return struct.unpack('<d', b)[0] if b else None


def _is_heap(v: int) -> bool:
    return 0x00400000 <= v < 0xFFFF0000


# ── Table traversal ──────────────────────────────────────────────────────────

def _tab_find_strkey(h: int, tab_ptr: int, key: str) -> int | None:
    """Return address of val TValue for string key, or None."""
    key_b    = key.encode()
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask    = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return None
    total = (hmask + 1) * _NODE_SIZE
    if total > 16 * 1024 * 1024:
        return None
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return None
    for i in range(hmask + 1):
        off    = i * _NODE_SIZE
        key_it = struct.unpack_from('<I', bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        gcs = struct.unpack_from('<I', bulk, off + 8)[0]
        if not _is_heap(gcs):
            continue
        slen_raw = _rpm(h, gcs + 12, 4)
        if not slen_raw:
            continue
        slen = struct.unpack('<I', slen_raw)[0]
        if slen != len(key_b):
            continue
        raw = _rpm(h, gcs + 16, slen)
        if raw == key_b:
            return node_ptr + off
    return None


def _tab_get_table(h: int, tab_ptr: int, key: str) -> int | None:
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    if _ru32(h, node + 4) != _LJ_TTAB:
        return None
    v = _ru32(h, node)
    return v if (v and _is_heap(v)) else None


def _tab_get_number(h: int, tab_ptr: int, key: str) -> float | None:
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = _ru32(h, node + 4)
    if it is None or it >= _LJ_TNUMX:
        return None
    return _rf64(h, node)


def _tab_get_string(h: int, tab_ptr: int, key: str) -> str | None:
    """Look up a string key and return its string value, or None."""
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = _ru32(h, node + 4)
    if it != _LJ_TSTR:
        return None
    gcs = _ru32(h, node)
    if not gcs or not _is_heap(gcs):
        return None
    slen_raw = _rpm(h, gcs + 12, 4)
    if not slen_raw:
        return None
    slen = struct.unpack('<I', slen_raw)[0]
    if slen > 256:
        return None
    raw = _rpm(h, gcs + 16, slen)
    if not raw:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _tab_array_get_table(h: int, tab_ptr: int, idx: int) -> int | None:
    """
    Return the GCtab* for array element [idx] (1-based) of a Lua table.

    LuaJIT 2.0.2 GCtab layout (32-bit):
      +8  MRef array  — pointer to array part
      +24 uint32 asize — number of array slots

    Each TValue is 8 bytes: [value:u32][itype:u32].
    Array element [i] (1-based) lives at array_ptr + (i-1)*8.
    """
    if idx < 1:
        return None
    asize = _ru32(h, tab_ptr + 24)
    if asize is None or idx > asize:
        return None
    array_ptr = _ru32(h, tab_ptr + 8)
    if not array_ptr or not _is_heap(array_ptr):
        return None
    offset = (idx - 1) * 8
    itype = _ru32(h, array_ptr + offset + 4)
    if itype != _LJ_TTAB:
        return None
    v = _ru32(h, array_ptr + offset)
    return v if (v and _is_heap(v)) else None


def _tab_get_bool(h: int, tab_ptr: int, key: str) -> bool | None:
    """Look up a string key and return True/False, or None if missing."""
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = _ru32(h, node + 4)
    if it == 0xFFFFFFFD:   # LJ_TTRUE
        return True
    if it == 0xFFFFFFFE:   # LJ_TFALSE
        return False
    return None


def _tab_iter_table_values(h: int, tab_ptr: int) -> list[int]:
    """Return GCtab* addresses for all table-valued entries (hash part)."""
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask    = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return []
    total = (hmask + 1) * _NODE_SIZE
    if total > 16 * 1024 * 1024:
        return []
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return []
    results: list[int] = []
    for i in range(hmask + 1):
        off    = i * _NODE_SIZE
        val_it = struct.unpack_from('<I', bulk, off + 4)[0]
        if val_it != _LJ_TTAB:
            continue
        val_lo = struct.unpack_from('<I', bulk, off)[0]
        if _is_heap(val_lo):
            results.append(val_lo)
    return results


# LuaJIT itype constants for bool values
_LJ_TTRUE  = 0xFFFFFFFD
_LJ_TFALSE = 0xFFFFFFFE
_LJ_TNIL   = 0xFFFFFFFF


def _tab_dump_flat(
    h: int,
    tab_ptr: int,
    prefix: str = "",
) -> dict[str, str | float | bool]:
    """
    Scan the hash part of one GCtab level and return every entry whose key is
    a string and whose value is a string, number, or boolean.

    ``prefix`` is prepended to every key (used when recursing into sub-tables).
    """
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask    = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return {}
    total = (hmask + 1) * _NODE_SIZE
    if total > 4 * 1024 * 1024:
        return {}
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return {}

    out: dict[str, str | float | bool] = {}
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        key_it  = struct.unpack_from('<I', bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        key_gcs = struct.unpack_from('<I', bulk, off + 8)[0]
        if not _is_heap(key_gcs):
            continue
        slen_b = _rpm(h, key_gcs + 12, 4)
        if not slen_b:
            continue
        slen = struct.unpack('<I', slen_b)[0]
        if slen == 0 or slen > 128:
            continue
        key_raw = _rpm(h, key_gcs + 16, slen)
        if not key_raw:
            continue
        try:
            key = prefix + key_raw.decode('utf-8')
        except UnicodeDecodeError:
            continue

        val_it = struct.unpack_from('<I', bulk, off + 4)[0]
        val_lo = struct.unpack_from('<I', bulk, off)[0]

        if val_it == _LJ_TTRUE:
            out[key] = True
        elif val_it == _LJ_TFALSE:
            out[key] = False
        elif val_it == _LJ_TSTR:
            if not _is_heap(val_lo):
                continue
            vslen_b = _rpm(h, val_lo + 12, 4)
            if not vslen_b:
                continue
            vslen = struct.unpack('<I', vslen_b)[0]
            if vslen == 0 or vslen > 256:
                continue
            val_raw = _rpm(h, val_lo + 16, vslen)
            if not val_raw:
                continue
            try:
                out[key] = val_raw.decode('utf-8')
            except UnicodeDecodeError:
                pass
        elif val_it < _LJ_TNUMX:
            raw8 = bulk[off:off + 8]
            try:
                out[key] = struct.unpack_from('<d', raw8)[0]
            except struct.error:
                pass

    return out


# Sub-tables worth recursing into for a complete entity snapshot.
_ENTITY_SUBTABLES = (
    "stats",        # str/dex/mag/wil/cun/con base stats
    "resists",      # damage type resistances  (keyed by DamageType int)
    "combat",       # dam/atk/apr/damspeed/dammod
    "inc_damage",   # % damage bonuses by type
    "resists_pen",  # penetration values
)


def _tab_dump_all(h: int, tab_ptr: int) -> dict[str, str | float | bool]:
    """
    Full entity field snapshot.

    Reads every flat string/number/bool from the entity table, then recurses
    one level into known sub-tables (stats, resists, combat, inc_damage,
    resists_pen), prefixing their keys as ``"subtable.key"``.
    """
    out = _tab_dump_flat(h, tab_ptr)

    for sub in _ENTITY_SUBTABLES:
        sub_ptr = _tab_get_table(h, tab_ptr, sub)
        if sub_ptr:
            out.update(_tab_dump_flat(h, sub_ptr, prefix=f"{sub}."))

    return out


# ── Process / region helpers ─────────────────────────────────────────────────

def _get_pid(name: str) -> int | None:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def _iter_regions(h: int):
    addr = 0
    mbi  = _MBI()
    while True:
        ret = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        ok   = (mbi.State == MEM_COMMIT
                and not (mbi.Protect & PAGE_NOACCESS)
                and not (mbi.Protect & PAGE_GUARD)
                and size > 0)
        if ok:
            data = _rpm(h, base, size)
            if data:
                yield base, data
        addr = base + size
        if addr >= 0xFFFFFFFF:
            break


def _find_global_table(h: int) -> int | None:
    """Scan for a large GCtab (gct=0x0B) containing game.player."""
    candidates: list[int] = []
    for base, data in _iter_regions(h):
        dlen = len(data)
        for off in range(0, dlen - 32, 4):
            if data[off + 5] != _GCT_TAB:
                continue
            if off + 32 > dlen:
                continue
            node_ptr = struct.unpack_from('<I', data, off + 20)[0]
            hmask    = struct.unpack_from('<I', data, off + 28)[0]
            if hmask < 63 or hmask > 0xFFFF:
                continue
            if not _is_heap(node_ptr):
                continue
            candidates.append(base + off)

    # Check candidates for the full game → player chain
    for addr in candidates:
        game_tab = _tab_get_table(h, addr, "game")
        if game_tab is None:
            continue
        player_tab = _tab_get_table(h, game_tab, "player")
        if player_tab is not None:
            return addr

    # Fallback: return first table with "game" key (player might load later)
    for addr in candidates:
        if _tab_find_strkey(h, addr, "game") is not None:
            return addr
    return None


# ── Entity data ───────────────────────────────────────────────────────────────

# ToME rank values (numeric)
RANK_CRITTER    = 1
RANK_NORMAL     = 2
RANK_ELITE      = 3
RANK_RARE       = 3.2    # may vary
RANK_UNIQUE     = 3.5
RANK_BOSS       = 4
RANK_ELITE_BOSS = 5

RANK_NAMES: dict[int, str] = {
    1: "Critter", 2: "Normal", 3: "Elite", 4: "Boss", 5: "Elite Boss",
}


def _rank_label(rank: float | None) -> str:
    if rank is None:
        return "Unknown"
    r = int(rank)
    if r in RANK_NAMES:
        return RANK_NAMES[r]
    if rank >= 3.5:
        return "Unique"
    if rank >= 3.2:
        return "Rare"
    if rank >= 3:
        return "Elite"
    return RANK_NAMES.get(r, f"Rank {rank:.1f}")


from dataclasses import dataclass


@dataclass(slots=True)
class PlayerStats:
    """Snapshot of the player's combat-relevant stats."""
    level: float
    max_life: float
    armor: float
    defense: float
    phys_save: float
    spell_save: float
    mental_save: float


# ── Danger rating ────────────────────────────────────────────────────────────

# Rank → weight for danger calculation (higher = scarier)
_RANK_WEIGHT: dict[int, float] = {
    1: 0.2,   # critter
    2: 1.0,   # normal
    3: 1.8,   # elite
    4: 3.0,   # boss
    5: 4.0,   # elite boss
}

DANGER_TRIVIAL   = "Trivial"
DANGER_EASY      = "Easy"
DANGER_MODERATE  = "Moderate"
DANGER_DANGEROUS = "Dangerous"
DANGER_DEADLY    = "Deadly"


def _rank_weight(rank: float) -> float:
    r = int(rank)
    if r in _RANK_WEIGHT:
        w = _RANK_WEIGHT[r]
    else:
        w = 1.0
    # Fractional ranks (3.2=rare, 3.5=unique) interpolate upward
    if rank >= 3.5:
        w = max(w, 2.4)   # unique
    elif rank >= 3.2:
        w = max(w, 2.0)   # rare
    return w


def compute_danger(enemy: "EntityInfo", player: PlayerStats | None) -> tuple[str, float]:
    """
    Compute a danger label and numeric score for an enemy relative to the
    player.  Returns (label, score).  Higher score = more dangerous.

    If player stats are unavailable, falls back to rank-only assessment.
    """
    if player is None or player.level <= 0:
        # Fallback: rank-only
        score = _rank_weight(enemy.rank) * 10 + enemy.level
        if score > 40:
            return DANGER_DEADLY, score
        if score > 25:
            return DANGER_DANGEROUS, score
        if score > 15:
            return DANGER_MODERATE, score
        if score > 8:
            return DANGER_EASY, score
        return DANGER_TRIVIAL, score

    rw = _rank_weight(enemy.rank)

    # Level delta: positive = enemy is higher level
    level_delta = enemy.level - player.level
    # Normalise to a -1..+1ish range, but allow > 1 for big gaps
    level_factor = level_delta / 5.0   # +5 levels = +1.0, -5 = -1.0

    # HP ratio: how tanky is the enemy compared to you
    hp_ratio = (enemy.max_life / player.max_life) if player.max_life > 0 else 1.0
    hp_factor = min(hp_ratio, 5.0) / 2.0   # cap at 5x, normalise ~0..2.5

    # Save advantage: average of enemy saves minus average of player saves
    enemy_avg_save = (enemy.phys_save + enemy.spell_save + enemy.mental_save) / 3.0
    player_avg_save = (player.phys_save + player.spell_save + player.mental_save) / 3.0
    save_delta = (enemy_avg_save - player_avg_save) / 15.0  # ~+-1 range

    # Defense advantage
    enemy_def = enemy.armor + enemy.defense
    player_def = player.armor + player.defense
    def_delta = (enemy_def - player_def) / 20.0  # ~+-1 range

    # Composite score:
    #   rank_weight is the anchor (1.0 for normal, 3.0 for boss)
    #   modifiers shift it based on relative stats
    raw = rw * (1.0 + 0.4 * level_factor + 0.2 * hp_factor
                + 0.1 * save_delta + 0.1 * def_delta)

    # Clamp to reasonable range
    score = max(0.0, raw)

    # Thresholds tuned so:
    #   same-level normal ≈ 1.0 → Easy
    #   same-level boss ≈ 3.0 → Dangerous
    #   +5 level boss ≈ 4.2+ → Deadly
    #   -5 level normal ≈ 0.6 → Trivial
    if score >= 3.5:
        return DANGER_DEADLY, score
    if score >= 2.2:
        return DANGER_DANGEROUS, score
    if score >= 1.3:
        return DANGER_MODERATE, score
    if score >= 0.7:
        return DANGER_EASY, score
    return DANGER_TRIVIAL, score


@dataclass(slots=True)
class EntityInfo:
    """Snapshot of one actor from game.level.entities."""
    name: str
    rank: float
    rank_label: str
    level: float
    life: float
    max_life: float
    faction: str
    x: float
    y: float
    armor: float
    defense: float
    phys_save: float
    spell_save: float
    mental_save: float
    danger: str          # label: Trivial / Easy / Moderate / Dangerous / Deadly
    danger_score: float  # numeric score for sorting
    image: str           # "npc/xxx.png" read directly from entity table, or ""
    # Extended fields
    type_name: str       # e.g. "insect"
    subtype: str         # e.g. "ant"
    size_category: float # 1=tiny … 5=huge
    unique: bool         # True if a named/random unique
    # Full flat field dump (strings, numbers, bools) — for debug / tooltip
    all_fields: dict[str, str | float | bool]


# ── Public API ────────────────────────────────────────────────────────────────

class MemoryReader:
    """Reads live game state from t-engine.exe via ReadProcessMemory."""

    def __init__(self) -> None:
        self._handle: int = 0
        self._pid: int = 0
        self._global_table: int = 0   # _G GCtab address
        self._player_table: int = 0   # game.player GCtab address (cached)

    @property
    def attached(self) -> bool:
        return self._handle != 0 and self._global_table != 0

    def attach(self) -> bool:
        """Find t-engine.exe and locate _G. Returns True on success."""
        self.detach()

        pid = _get_pid("t-engine.exe")
        if pid is None:
            return False

        h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            return False

        self._handle = h
        self._pid    = pid

        gt = _find_global_table(h)
        if gt is None:
            self.detach()
            return False

        self._global_table = gt
        self._player_table = 0  # will be resolved on first read
        return True

    def detach(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
        self._handle = 0
        self._pid = 0
        self._global_table = 0
        self._player_table = 0

    def is_process_alive(self) -> bool:
        if not self._pid:
            return False
        return _get_pid("t-engine.exe") == self._pid

    def read_player_hp(self) -> tuple[float, float] | None:
        """Return (life, max_life) or None if unavailable."""
        if not self.attached:
            return None

        h  = self._handle
        gt = self._global_table

        # Resolve player table (may change on save load)
        game_tab = _tab_get_table(h, gt, "game")
        if game_tab is None:
            return None

        player_tab = _tab_get_table(h, game_tab, "player")
        if player_tab is None:
            self._player_table = 0
            return None
        self._player_table = player_tab

        life     = _tab_get_number(h, player_tab, "life")
        max_life = _tab_get_number(h, player_tab, "max_life")
        if life is None or max_life is None:
            return None
        return life, max_life

    def read_player_mana(self) -> tuple[float, float] | None:
        """Return (mana, max_mana) or None if character has no mana."""
        if not self._player_table or not self.attached:
            return None
        h  = self._handle
        pt = self._player_table
        mana     = _tab_get_number(h, pt, "mana")
        max_mana = _tab_get_number(h, pt, "max_mana")
        if mana is None or max_mana is None or max_mana <= 0:
            return None
        return mana, max_mana

    def read_level_id(self) -> str | None:
        """Return game.level.id string, or None."""
        if not self.attached:
            return None
        h  = self._handle
        gt = self._global_table

        game_tab = _tab_get_table(h, gt, "game")
        if game_tab is None:
            return None
        level_tab = _tab_get_table(h, game_tab, "level")
        if level_tab is None:
            return None
        return _tab_get_string(h, level_tab, "id")

    def read_player_stats(self) -> PlayerStats | None:
        """Read the player's combat-relevant stats for danger comparison."""
        if not self._player_table or not self.attached:
            self.read_player_hp()
        if not self._player_table:
            return None

        h  = self._handle
        pt = self._player_table

        level = _tab_get_number(h, pt, "level")
        if level is None:
            return None

        return PlayerStats(
            level=level,
            max_life=_tab_get_number(h, pt, "max_life") or 0.0,
            armor=_tab_get_number(h, pt, "combat_armor") or 0.0,
            defense=_tab_get_number(h, pt, "combat_def") or 0.0,
            phys_save=_tab_get_number(h, pt, "combat_physresist") or 0.0,
            spell_save=_tab_get_number(h, pt, "combat_spellresist") or 0.0,
            mental_save=_tab_get_number(h, pt, "combat_mentalresist") or 0.0,
        )

    def read_entities(self, min_rank: float = 1.5) -> list[EntityInfo]:
        """
        Read all actors from game.level.entities with rank > min_rank.
        Excludes dead actors and the player.  Computes a danger rating
        relative to the current player.  Returns a list sorted by danger
        score (most dangerous first).
        """
        if not self.attached:
            return []
        h  = self._handle
        gt = self._global_table

        game_tab = _tab_get_table(h, gt, "game")
        if game_tab is None:
            return []
        level_tab = _tab_get_table(h, game_tab, "level")
        if level_tab is None:
            return []
        entities_tab = _tab_get_table(h, level_tab, "entities")
        if entities_tab is None:
            return []

        player_tab = _tab_get_table(h, game_tab, "player")
        player_stats = self.read_player_stats()

        actor_ptrs = _tab_iter_table_values(h, entities_tab)
        results: list[EntityInfo] = []

        for ptr in actor_ptrs:
            # Skip the player
            if ptr == player_tab:
                continue

            # Skip dead actors
            dead = _tab_get_bool(h, ptr, "dead")
            if dead is True:
                continue

            # Rank filter
            rank = _tab_get_number(h, ptr, "rank")
            if rank is not None and rank <= min_rank:
                continue

            # Dump all flat fields first — single pass over the hash table
            all_fields = _tab_dump_all(h, ptr)

            name      = (all_fields.get("name") or "?") if isinstance(all_fields.get("name"), str) else "?"
            life      = float(all_fields.get("life") or 0.0)
            max_life  = float(all_fields.get("max_life") or 0.0)
            level     = float(all_fields.get("level") or 0.0)
            faction   = (all_fields.get("faction") or "?") if isinstance(all_fields.get("faction"), str) else "?"
            type_name = (all_fields.get("type") or "") if isinstance(all_fields.get("type"), str) else ""
            subtype   = (all_fields.get("subtype") or "") if isinstance(all_fields.get("subtype"), str) else ""
            size_cat  = float(all_fields.get("size_category") or 0.0)
            unique    = bool(all_fields.get("unique", False))

            # ── Image resolution ──────────────────────────────────────────
            # Three patterns in ToME:
            #   Direct:           image = "npc/troll_f.png"  → use directly.
            #   nice_tile:        image = "invis.png"; real path in add_mos[1].image.
            #   attachement_spots: string field holding "npc/xxx.png" (random bosses).
            raw_image = str(all_fields.get("image") or "")
            if raw_image.startswith("npc/") and raw_image != "invis.png":
                entity_image = raw_image
            else:
                # Try add_mos[1].image first
                entity_image = ""
                add_mos_tab = _tab_get_table(h, ptr, "add_mos")
                if add_mos_tab:
                    first = _tab_array_get_table(h, add_mos_tab, 1)
                    if first:
                        mos_img = _tab_get_string(h, first, "image") or ""
                        if mos_img.startswith("npc/"):
                            entity_image = mos_img
                # Fall back to attachement_spots (typo is in the game source)
                if not entity_image:
                    attach = str(all_fields.get("attachement_spots") or "")
                    if attach.startswith("npc/"):
                        entity_image = attach

            if not entity_image:
                import sys
                has_mos = "yes" if _tab_get_table(h, ptr, "add_mos") else "no"
                print(f"[sprite] {name!r}: raw_image={raw_image!r}, add_mos={has_mos}", file=sys.stderr)
                print(f"[fields] {name!r}: " + ", ".join(
                    f"{k}={v!r}" for k, v in sorted(all_fields.items())
                    if k in ("type", "subtype", "image", "unique", "size_category",
                             "ai", "autolevel", "rank", "level")
                ), file=sys.stderr)

            ent = EntityInfo(
                name=name,
                rank=rank or 0.0,
                rank_label=_rank_label(rank),
                level=level,
                life=life,
                max_life=max_life,
                faction=faction,
                x=float(all_fields.get("x") or 0.0),
                y=float(all_fields.get("y") or 0.0),
                armor=float(all_fields.get("combat_armor") or 0.0),
                defense=float(all_fields.get("combat_def") or 0.0),
                phys_save=float(all_fields.get("combat_physresist") or 0.0),
                spell_save=float(all_fields.get("combat_spellresist") or 0.0),
                mental_save=float(all_fields.get("combat_mentalresist") or 0.0),
                danger="",
                danger_score=0.0,
                image=entity_image,
                type_name=type_name,
                subtype=subtype,
                size_category=size_cat,
                unique=unique,
                all_fields=all_fields,
            )
            ent.danger, ent.danger_score = compute_danger(ent, player_stats)
            results.append(ent)

        # Sort: most dangerous first
        results.sort(key=lambda e: -e.danger_score)
        return results

    def read_player_resources(self) -> dict[str, float]:
        """Read common player resources. Returns {name: value} dict."""
        if not self._player_table or not self.attached:
            # Trigger a player table resolve
            self.read_player_hp()

        if not self._player_table:
            return {}

        h  = self._handle
        pt = self._player_table

        keys = [
            "life", "max_life",
            "mana", "max_mana",
            "stamina", "max_stamina",
            "vim", "max_vim",
            "positive", "max_positive",
            "negative", "max_negative",
            "psi", "max_psi",
            "hate", "max_hate",
            "paradox",
            "equilibrium",
            "money",
        ]
        result: dict[str, float] = {}
        for key in keys:
            val = _tab_get_number(h, pt, key)
            if val is not None:
                result[key] = val
        return result
