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
import json
import re
import struct
import sys
import threading as _threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from game_data.effect_db import lookup_effect_by_id
from game_data.prodigy_db import get_prodigy_db as _get_prodigy_db
from game_data.talent_db import lookup_talent_by_id
from gui.sprite_resolve import is_usable_sprite, pick_actor_image
from runtime_output import console_print

# Re-exported so ``from gui.memory_reader import DANGER_*`` keeps working
# for gui.enemy_panel without forcing every consumer to reach into
# :mod:`scoring.ranks` directly.
from scoring.ranks import (
    DANGER_DANGEROUS,
    DANGER_DEADLY,
    DANGER_EASY,
    DANGER_MODERATE,
    DANGER_TRIVIAL,
)
from scoring.ranks import compute_danger as _compute_danger_ranked
from scoring.ranks import rank_label as _rank_label

__all__ = (
    "DANGER_DANGEROUS",
    "DANGER_DEADLY",
    "DANGER_EASY",
    "DANGER_MODERATE",
    "DANGER_TRIVIAL",
    "EntityInfo",
    "MemoryReader",
    "PlayerStats",
    "compute_danger",
    "is_process_running",
    "start_background_preattach",
    "take_preattached_reader",
)

# ── Win32 constants ───────────────────────────────────────────────────────────
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

_k32 = ctypes.windll.kernel32
_psapi = ctypes.WinDLL("Psapi.dll")
_ATTACH_CACHE_FILE = Path(__file__).with_name("_attach_cache.json")
_GAME_TABLE_REVALIDATE_INTERVAL = 60


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.wintypes.DWORD),
        ("Protect", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
    ]


# ── LuaJIT 2.0.2 constants (32-bit, no GC64) ────────────────────────────────
#
# GC header gct values:  GCstr=4, GCtab=11 (0x0B), lua_State=6
# TValue itype values:   LJ_TSTR=0xFFFFFFFB, LJ_TTAB=0xFFFFFFF4
#                         number: itype < 0xFFFFFFF2

_GCT_TAB = 0x0B
_LJ_TSTR = 0xFFFFFFFB
_LJ_TTAB = 0xFFFFFFF4
_LJ_TNUMX = 0xFFFFFFF2
_LJ_TTRUE = 0xFFFFFFFD
_LJ_TFALSE = 0xFFFFFFFE
_LJ_TNIL = 0xFFFFFFFF
_NODE_SIZE = 24

_PLAYER_BASE_STAT_ORDER: tuple[str, ...] = (
    "str",
    "dex",
    "mag",
    "wil",
    "cun",
    "con",
    "lck",
)

_DEFAULT_RESIST_CAP = 70.0
_QUEST_COMPLETED = 1
_QUEST_DONE = 100
_QUEST_FAILED = 101
_BASE_DAMAGE_TYPE_IDS: dict[str, int] = {
    "PHYSICAL": 1,
    "ARCANE": 2,
    "FIRE": 3,
    "COLD": 4,
    "LIGHTNING": 5,
    "ACID": 6,
    "NATURE": 7,
    "BLIGHT": 8,
    "LIGHT": 9,
    "DARKNESS": 10,
    "MIND": 11,
    "TEMPORAL": 12,
}
_RE_REQ_DAMAGE_LOG = re.compile(r"self\.(damage(?:_intake)?_log)")
_RE_REQ_DAMAGE_TYPE = re.compile(r"DamageType\.(\w+)")
_RE_REQ_GE = re.compile(r">=\s*(\d+)")
_RE_REQ_ATTR = re.compile(r'self:attr\("([^"]+)"\)(?:\s*and\s*self:attr\("[^"]+"\)\s*>=\s*(\d+))?')
_RE_REQ_ATTR_MUST_BE_FALSE = re.compile(r'if\s+self:attr\("([^"]+)"\)\s+then\s+return\s+false\s+end')
_RE_REQ_ATTR_MUST_BE_TRUE = re.compile(r'if\s+not\s+self:attr\("([^"]+)"\)\s+then\s+return\s+false\s+end')
_RE_REQ_ALLOW_BUILD = re.compile(r"profile\.mod\.allow_build\.([A-Za-z0-9_]+)")
_RE_REQ_TALENT_KIND = re.compile(r"self\.talent_kind_log\.(\w+)\s*>=\s*(\d+)")
_RE_REQ_KNOW_TALENT = re.compile(r"self:knowTalent\(self\.(T_[A-Z0-9_]+)\)")
_RE_REQ_TALENT_RAW = re.compile(r"self:getTalentLevelRaw\(self\.(T_[A-Z0-9_]+)\)\s*>=\s*(\d+)")
_RE_REQ_LUCK = re.compile(r"self:getLck\(\)\s*>=\s*(\d+)")
_RE_REQ_SIZE = re.compile(r"self\.size_category\s*and\s*self\.size_category\s*>=\s*(\d+)")
_RE_REQ_SELF_FIELD = re.compile(r"self\.([A-Za-z_][A-Za-z0-9_]*)\s*and\s*self\.\1\s*>=\s*(\d+)")
_RE_REQ_COMBAT_DEF = re.compile(r"self:combatDefense\(\)\s*>=\s*(\d+)")
_RE_REQ_QUEST_ID = re.compile(r'self:(?:hasQuest|isQuestStatus)\("([^"]+)"')
_RE_REQ_QUEST_STATUS = re.compile(
    r"(not\s+)?(?:self:isQuestStatus\(\"[^\"]+\"|"
    r"self:hasQuest\(\"[^\"]+\"\):isStatus)\("
    r"engine\.Quest\.(DONE|COMPLETED|FAILED)(?:,\s*\"([^\"]+)\")?\)"
)
_RE_REQ_QUEST_COMPLETED = re.compile(r'(not\s+)?[A-Za-z_][A-Za-z0-9_]*:isCompleted\("([^"]+)"\)')
_RE_REQ_INSCRIPTION = re.compile(r"inscription_(restrictions|forbids)\['([^']+)'\]")

_ENTITY_ROOT_FIELDS = {
    "attachement_spots",
    "combat_armor",
    "combat_def",
    "combat_mentalresist",
    "combat_physresist",
    "combat_spellresist",
    "faction",
    "global_speed",
    "image",
    "level",
    "life",
    "max_life",
    "name",
    "rank",
    "size_category",
    "subtype",
    "type",
    "unique",
    "x",
    "y",
}

_ENTITY_COMBAT_FIELDS = {
    "apr",
    "atk",
    "crit",
    "crit_power",
    "dam",
    "physspeed",
}

_ENTITY_RESIST_FIELDS = {
    "all",
    "ARCANE",
    "BLIGHT",
    "COLD",
    "DARKNESS",
    "FIRE",
    "LIGHT",
    "LIGHTNING",
    "NATURE",
    "PHYSICAL",
    "TEMPORAL",
}


# ── Low-level memory access ──────────────────────────────────────────────────


def _rpm(h: int, addr: int, n: int) -> bytes | None:
    buf = ctypes.create_string_buffer(n)
    read = ctypes.c_size_t(0)
    ok = _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(read))
    return bytes(buf) if (ok and read.value == n) else None


def _ru32(h: int, addr: int) -> int | None:
    b = _rpm(h, addr, 4)
    return struct.unpack("<I", b)[0] if b else None


def _rf64(h: int, addr: int) -> float | None:
    b = _rpm(h, addr, 8)
    return struct.unpack("<d", b)[0] if b else None


def _is_heap(v: int) -> bool:
    return 0x00400000 <= v < 0xFFFF0000


def _is_gctab(h: int, tab_ptr: int) -> bool:
    """Return True when ``tab_ptr`` still looks like a LuaJIT GCtab."""
    if not tab_ptr or not _is_heap(tab_ptr):
        return False
    raw = _rpm(h, tab_ptr, 32)
    if not raw or raw[5] != _GCT_TAB:
        return False
    array_ptr = struct.unpack_from("<I", raw, 8)[0]
    node_ptr = struct.unpack_from("<I", raw, 20)[0]
    hmask = struct.unpack_from("<I", raw, 28)[0]
    if hmask > 0xFFFF:
        return False
    if array_ptr and not _is_heap(array_ptr):
        return False
    if node_ptr and not _is_heap(node_ptr):
        return False
    return True


# ── Table traversal ──────────────────────────────────────────────────────────


def _tab_find_strkey(h: int, tab_ptr: int, key: str) -> int | None:
    """Return address of val TValue for string key, or None."""
    key_b = key.encode()
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return None
    total = (hmask + 1) * _NODE_SIZE
    if total > 16 * 1024 * 1024:
        return None
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return None
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        if not _is_heap(gcs):
            continue
        slen_raw = _rpm(h, gcs + 12, 4)
        if not slen_raw:
            continue
        slen = struct.unpack("<I", slen_raw)[0]
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
    slen = struct.unpack("<I", slen_raw)[0]
    if slen > 256:
        return None
    raw = _rpm(h, gcs + 16, slen)
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
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


def _tab_array_get_number(h: int, tab_ptr: int, idx: int) -> float | None:
    """Return the numeric value for array element ``[idx]`` (1-based)."""
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
    if itype is None or itype >= _LJ_TNUMX:
        return None
    raw = _rpm(h, array_ptr + offset, 8)
    if not raw:
        return None
    try:
        return struct.unpack("<d", raw)[0]
    except struct.error:
        return None


def _tab_get_bool(h: int, tab_ptr: int, key: str) -> bool | None:
    """Look up a string key and return True/False, or None if missing."""
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = _ru32(h, node + 4)
    if it == 0xFFFFFFFD:  # LJ_TTRUE
        return True
    if it == 0xFFFFFFFE:  # LJ_TFALSE
        return False
    return None


def _tab_iter_table_values(h: int, tab_ptr: int) -> list[int]:
    """Return GCtab* addresses for all table-valued entries (hash part)."""
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
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
        off = i * _NODE_SIZE
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        if val_it != _LJ_TTAB:
            continue
        val_lo = struct.unpack_from("<I", bulk, off)[0]
        if _is_heap(val_lo):
            results.append(val_lo)
    return results


def _tab_iter_string_entries(h: int, tab_ptr: int, *, max_key_len: int = 256) -> list[tuple[str, int, int]]:
    """Return ``(key, value_itype, value_lo)`` for string-keyed hash entries."""
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return []
    total = (hmask + 1) * _NODE_SIZE
    if total > 16 * 1024 * 1024:
        return []
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return []

    entries: list[tuple[str, int, int]] = []
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        key_gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        if not _is_heap(key_gcs):
            continue
        slen_b = _rpm(h, key_gcs + 12, 4)
        if not slen_b:
            continue
        slen = struct.unpack("<I", slen_b)[0]
        if slen == 0 or slen > max_key_len:
            continue
        raw = _rpm(h, key_gcs + 16, slen)
        if not raw:
            continue
        try:
            key = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        val_lo = struct.unpack_from("<I", bulk, off)[0]
        entries.append((key, val_it, val_lo))
    return entries


def _tab_string_keys_with_true(h: int, tab_ptr: int, *, max_key_len: int = 256) -> set[str]:
    """Return string keys whose value is the Lua literal ``true``.

    ToME uses this "set of names" pattern all over ``game.state`` and
    ``game.visited_zones``: keys are strings, values are just ``true``.
    ``max_key_len`` caps how long a key we'll accept (zone names are
    short; NPC ids can run longer).
    """
    return {
        key
        for key, val_it, _ in _tab_iter_string_entries(h, tab_ptr, max_key_len=max_key_len)
        if val_it == _LJ_TTRUE
    }


def _tab_get_ordered_tables(h: int, tab_ptr: int) -> list[int]:
    """
    Return GCtab* pointers from a table whose keys are integers (1..N),
    sorted ascending by key. Used for add_mos which is keyed 1, 2, 3 …

    LuaJIT may store sequential integer keys in either:
      1. the array part (fast path for dense 1..N tables), or
      2. the hash part as 64-bit IEEE 754 doubles when the array part is absent.

    Hash nodes are 24 bytes:
      [val.lo u32][val.itype u32][key.lo u32][key.hi u32][next u32][pad u32]
    A numeric key has key.hi < _LJ_TNUMX; the full double is at key_lo:key_hi.
    """
    ordered: list[int] = []
    seen: set[int] = set()

    asize = _ru32(h, tab_ptr + 24)
    if asize:
        for idx in range(1, asize + 1):
            entry = _tab_array_get_table(h, tab_ptr, idx)
            if entry and entry not in seen:
                ordered.append(entry)
                seen.add(entry)

    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return ordered
    total = (hmask + 1) * _NODE_SIZE
    if total > 4 * 1024 * 1024:
        return ordered
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return ordered

    keyed: list[tuple[int, int]] = []
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        # Value must be a table
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        if val_it != _LJ_TTAB:
            continue
        val_lo = struct.unpack_from("<I", bulk, off)[0]
        if not _is_heap(val_lo):
            continue
        # Key must be a positive integer stored as a double
        key_hi = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_hi >= _LJ_TNUMX:
            continue  # not a number
        try:
            key_f = struct.unpack_from("<d", bulk, off + 8)[0]
        except struct.error:
            continue
        if key_f < 1 or key_f != int(key_f):
            continue
        keyed.append((int(key_f), val_lo))

    keyed.sort(key=lambda x: x[0])
    for _, ptr in keyed:
        if ptr not in seen:
            ordered.append(ptr)
            seen.add(ptr)
    return ordered




def _tab_dump_flat(
    h: int,
    tab_ptr: int,
    prefix: str = "",
    *,
    allowed_keys: set[str] | None = None,
) -> dict[str, str | float | bool]:
    """
    Scan the hash part of one GCtab level and return every entry whose key is
    a string and whose value is a string, number, or boolean.

    ``prefix`` is prepended to every key (used when recursing into sub-tables).
    """
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
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
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        key_gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        if not _is_heap(key_gcs):
            continue
        slen_b = _rpm(h, key_gcs + 12, 4)
        if not slen_b:
            continue
        slen = struct.unpack("<I", slen_b)[0]
        if slen == 0 or slen > 128:
            continue
        key_raw = _rpm(h, key_gcs + 16, slen)
        if not key_raw:
            continue
        try:
            base_key = key_raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if allowed_keys is not None and base_key not in allowed_keys:
            continue
        key = prefix + base_key

        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        val_lo = struct.unpack_from("<I", bulk, off)[0]

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
            vslen = struct.unpack("<I", vslen_b)[0]
            if vslen == 0 or vslen > 256:
                continue
            val_raw = _rpm(h, val_lo + 16, vslen)
            if not val_raw:
                continue
            try:
                out[key] = val_raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
        elif val_it < _LJ_TNUMX:
            raw8 = bulk[off : off + 8]
            try:
                out[key] = struct.unpack_from("<d", raw8)[0]
            except struct.error:
                pass

    return out


# Sub-tables worth recursing into for a complete entity snapshot.
_ENTITY_SUBTABLES = (
    "stats",  # str/dex/mag/wil/cun/con base stats
    "resists",  # damage type resistances
    "combat",  # dam/atk/apr/damspeed/dammod
    "inc_damage",  # % damage bonuses by type
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


def _tab_dump_entity_snapshot(h: int, actor_ptr: int) -> dict[str, str | float | bool]:
    """Return only the entity fields used by the enemy panel and threat math."""
    out = _tab_dump_flat(h, actor_ptr, allowed_keys=_ENTITY_ROOT_FIELDS)

    combat_tab = _tab_get_table(h, actor_ptr, "combat")
    if combat_tab:
        out.update(_tab_dump_flat(h, combat_tab, prefix="combat.", allowed_keys=_ENTITY_COMBAT_FIELDS))

    resists_tab = _tab_get_table(h, actor_ptr, "resists")
    if resists_tab:
        out.update(_tab_dump_flat(h, resists_tab, prefix="resists.", allowed_keys=_ENTITY_RESIST_FIELDS))

    inc_damage_tab = _tab_get_table(h, actor_ptr, "inc_damage")
    if inc_damage_tab:
        out.update(_tab_dump_flat(h, inc_damage_tab, prefix="inc_damage."))

    resists_pen_tab = _tab_get_table(h, actor_ptr, "resists_pen")
    if resists_pen_tab:
        out.update(_tab_dump_flat(h, resists_pen_tab, prefix="resists_pen."))

    return out


def _tab_get_number_by_index(h: int, tab_ptr: int, idx: int) -> float | None:
    """Return the numeric value stored at integer key ``idx``."""
    value = _tab_array_get_number(h, tab_ptr, idx)
    if value is not None:
        return value

    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return None
    total = (hmask + 1) * _NODE_SIZE
    if total > 4 * 1024 * 1024:
        return None
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return None

    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        if val_it >= _LJ_TNUMX:
            continue
        key_hi = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_hi >= _LJ_TNUMX:
            continue
        try:
            key_f = struct.unpack_from("<d", bulk, off + 8)[0]
            if key_f != idx:
                continue
            return struct.unpack_from("<d", bulk, off)[0]
        except struct.error:
            continue
    return None


def _tab_get_scalar(h: int, tab_ptr: int, key: str) -> str | float | bool | None:
    value = _tab_get_number(h, tab_ptr, key)
    if value is not None:
        return value
    value_bool = _tab_get_bool(h, tab_ptr, key)
    if value_bool is not None:
        return value_bool
    value_str = _tab_get_string(h, tab_ptr, key)
    if value_str is not None:
        return value_str
    return None


def _table_number_by_damage_type(h: int, tab_ptr: int, damage_type: str) -> float | None:
    value = _tab_get_number(h, tab_ptr, damage_type)
    if value is not None:
        return value
    idx = _BASE_DAMAGE_TYPE_IDS.get(damage_type.upper())
    if idx is None:
        return None
    return _tab_get_number_by_index(h, tab_ptr, idx)


def _format_requirement_text(requirements: list[str]) -> str | list[str]:
    if not requirements:
        return "Ready to learn"
    if len(requirements) == 1:
        return requirements[0]
    return requirements


def _tome_exp_chart(level: int, exp_mod: float = 1.0) -> float:
    """Mirror ToME's module-specific ``ActorLevel.exp_chart``."""
    exp = 10.0
    mult = 8.5
    min_mult = 3.0
    for _ in range(2, level + 1):
        exp += level * mult
        if level < 30:
            mult = max(min_mult, min(mult, mult - 0.2))
        else:
            mult = max(min_mult, min(mult, mult - 0.1))
    return float(int(exp * exp_mod + 0.999999))


def _stack_resists(all_resist: float, specific_resist: float) -> float:
    """Match ToME's combined all-resist + per-type resist formula."""
    a = max(-100.0, min(100.0, all_resist)) / 100.0
    b = max(-100.0, min(100.0, specific_resist)) / 100.0
    return 100.0 * (1.0 - (1.0 - a) * (1.0 - b))


def _effective_player_resists(
    raw_resists: dict[str, float],
    raw_caps: dict[str, float],
) -> dict[str, float]:
    """Return combat-effective player resist values (pre-penetration)."""
    if not raw_resists:
        return {}

    all_resist = raw_resists.get("all", 0.0)
    all_cap = raw_caps.get("all", _DEFAULT_RESIST_CAP)

    effective: dict[str, float] = {}
    if "all" in raw_resists:
        effective["all"] = max(-100.0, min(all_resist, all_cap))

    for dtype, resist in raw_resists.items():
        if dtype == "all":
            continue
        cap = all_cap + raw_caps.get(dtype, 0.0)
        effective[dtype] = max(-100.0, min(_stack_resists(all_resist, resist), cap))
    return effective


def _format_live_number(value: float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}".rstrip("0").rstrip(".")


def _display_engine_id(raw_id: str, prefix: str) -> str:
    label = raw_id.removeprefix(prefix).replace("_", " ").strip()
    return label.title() if label else raw_id


# ── Process / region helpers ─────────────────────────────────────────────────


def _get_pid(name: str) -> int | None:
    target = name.lower()
    for pid in _iter_process_ids():
        image_name = _get_process_image_name(pid)
        if image_name and image_name.lower() == target:
            return pid
    return None


def is_process_running(name: str) -> bool:
    """Return True when a process image with *name* is currently running."""
    return _get_pid(name) is not None


def _iter_process_ids() -> list[int]:
    count = 256
    while True:
        buffer = (ctypes.wintypes.DWORD * count)()
        needed = ctypes.wintypes.DWORD()
        if not _psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return []
        returned = needed.value // ctypes.sizeof(ctypes.wintypes.DWORD())
        if returned < count:
            return [int(buffer[index]) for index in range(returned) if buffer[index]]
        count *= 2


def _get_process_image_name(pid: int) -> str | None:
    process = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        size = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _k32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).name
    finally:
        _k32.CloseHandle(process)


def _get_process_creation_key(h: int) -> int | None:
    creation_time = ctypes.wintypes.FILETIME()
    exit_time = ctypes.wintypes.FILETIME()
    kernel_time = ctypes.wintypes.FILETIME()
    user_time = ctypes.wintypes.FILETIME()
    if not _k32.GetProcessTimes(
        h,
        ctypes.byref(creation_time),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    return (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime


def _load_attach_cache() -> dict[str, int] | None:
    try:
        data = json.loads(_ATTACH_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return {
            "pid": int(data["pid"]),
            "creation_key": int(data["creation_key"]),
            "global_table": int(data["global_table"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _save_attach_cache(pid: int, creation_key: int, global_table: int) -> None:
    try:
        _ATTACH_CACHE_FILE.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "creation_key": creation_key,
                    "global_table": global_table,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _iter_regions(h: int):
    addr = 0
    mbi = _MBI()
    while True:
        ret = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        ok = (
            mbi.State == MEM_COMMIT
            and not (mbi.Protect & PAGE_NOACCESS)
            and not (mbi.Protect & PAGE_GUARD)
            and size > 0
        )
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
            node_ptr = struct.unpack_from("<I", data, off + 20)[0]
            hmask = struct.unpack_from("<I", data, off + 28)[0]
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


def _validate_global_table(h: int, addr: int) -> int | None:
    """Return *addr* when it still looks like the Lua global table."""
    if not _is_gctab(h, addr):
        return None
    game_tab = _tab_get_table(h, addr, "game")
    if game_tab is None or not _validate_game_table(h, game_tab):
        return None
    return addr


def _validate_game_table(h: int, addr: int) -> bool:
    """Return True when ``addr`` is a plausible live ToME ``game`` singleton."""
    if not _is_gctab(h, addr):
        return False
    return any(
        _tab_find_strkey(h, addr, key) is not None
        for key in ("player", "level", "zone", "state", "visited_zones", "party", "turn")
    )


# ── Entity data ───────────────────────────────────────────────────────────────


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
    # Extended fields for threat-math (scoring/enemy_threat). All default
    # to safe zero values so older callers still work.
    die_at: float = 0.0
    armor_hardiness: float = 0.0
    evasion: float = 0.0
    ignore_direct_crits: float = 0.0
    resists: dict[str, float] = field(default_factory=dict)
    resists_pen: dict[str, float] = field(default_factory=dict)
    resists_cap: dict[str, float] = field(default_factory=dict)


_INVENTORY_BUCKET_SLOT_LABELS: dict[str, str] = {
    "MAINHAND": "Mainhand",
    "OFFHAND": "Offhand",
    "BODY": "Body",
    "HEAD": "Head",
    "HANDS": "Hands",
    "FEET": "Feet",
    "BELT": "Belt",
    "NECK": "Neck",
    "LITE": "Lite",
    "TOOL": "Tool",
    "QUIVER": "Quiver",
    "FINGER": "Ring",
}


# ── Danger rating ────────────────────────────────────────────────────────────
#
# The math lives in :mod:`scoring.ranks`. Here we only need a thin adapter —
# :class:`EntityInfo` and :class:`PlayerStats` already expose the fields the
# ranks module's private dataclasses need, so duck-typing is enough.


def compute_danger(enemy: EntityInfo, player: PlayerStats | None) -> tuple[str, float]:
    """Return ``(label, score)`` for ``enemy`` relative to ``player``."""
    return _compute_danger_ranked(enemy, player)  # type: ignore[arg-type]


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
    danger: str  # label: Trivial / Easy / Moderate / Dangerous / Deadly
    danger_score: float  # numeric score for sorting
    image: str  # representative single sprite path, or ""
    sprite_layers: list[str]  # ordered add_mos layer paths for compositing (may be empty)
    # Extended fields
    type_name: str  # e.g. "insect"
    subtype: str  # e.g. "ant"
    size_category: float  # 1=tiny … 5=huge
    unique: bool  # True if a named/random unique
    # Compact flat field snapshot used by the enemy panel / threat math
    all_fields: dict[str, str | float | bool]


# ── Public API ────────────────────────────────────────────────────────────────


class MemoryReader:
    """Reads live game state from t-engine.exe via ReadProcessMemory."""

    def __init__(self) -> None:
        self._handle: int = 0
        self._pid: int = 0
        self._global_table: int = 0  # _G GCtab address
        self._game_table: int = 0  # _G.game GCtab address (cached singleton)
        self._game_table_reads_until_validate: int = 0
        self._player_table: int = 0  # game.player GCtab address (cached)

    @property
    def attached(self) -> bool:
        return self._handle != 0 and self._global_table != 0

    def attach(self) -> bool:
        """Find t-engine.exe and locate _G. Returns True on success."""
        self.detach()

        pid = _get_pid("t-engine.exe")
        if pid is None:
            return False

        _k32.SetLastError(0)
        h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            err = _k32.GetLastError()
            limited = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if limited:
                _k32.CloseHandle(limited)
            if err == 5 and limited:
                print(
                    "[memory] Access denied opening t-engine.exe for VM_READ. "
                    "The monitor is not elevated high enough to read game memory.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[memory] OpenProcess failed for t-engine.exe (pid={pid}, error={err}).",
                    file=sys.stderr,
                )
            return False

        self._handle = h
        self._pid = pid

        creation_key = _get_process_creation_key(h)
        if creation_key is not None:
            cache = _load_attach_cache()
            if cache and cache["pid"] == pid and cache["creation_key"] == creation_key:
                if cached_gt := _validate_global_table(h, cache["global_table"]):
                    console_print("[memory] Reused cached Lua global table address.")
                    self._global_table = cached_gt
                    self._game_table = _tab_get_table(h, cached_gt, "game") or 0
                    self._game_table_reads_until_validate = _GAME_TABLE_REVALIDATE_INTERVAL
                    self._player_table = 0
                    return True

        gt = _find_global_table(h)
        if gt is None:
            print("[memory] OpenProcess succeeded, but the Lua global table scan found no match.", file=sys.stderr)
            self.detach()
            return False

        self._global_table = gt
        self._game_table = _tab_get_table(h, gt, "game") or 0
        self._game_table_reads_until_validate = _GAME_TABLE_REVALIDATE_INTERVAL
        self._player_table = 0  # will be resolved on first read
        if creation_key is not None:
            _save_attach_cache(pid, creation_key, gt)
        return True

    def detach(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
        self._handle = 0
        self._pid = 0
        self._global_table = 0
        self._game_table = 0
        self._game_table_reads_until_validate = 0
        self._player_table = 0

    def is_process_alive(self) -> bool:
        if not self._pid:
            return False
        return _get_pid("t-engine.exe") == self._pid

    def _ensure_player_table(self) -> int | None:
        """Return the cached ``game.player`` table, refreshing it when needed."""
        if not self.attached:
            return None

        h = self._handle
        if self._player_table and _tab_get_number(h, self._player_table, "level") is not None:
            return self._player_table

        game_tab = self._ensure_game_table()
        if game_tab is None:
            self._player_table = 0
            return None

        player_tab = _tab_get_table(h, game_tab, "player")
        if player_tab is None:
            game_tab = self._ensure_game_table(force_validate=True)
            player_tab = _tab_get_table(h, game_tab, "player") if game_tab is not None else None
            if player_tab is None:
                self._player_table = 0
                return None

        self._player_table = player_tab
        return player_tab

    def _ensure_game_table(self, *, force_validate: bool = False) -> int | None:
        """Return the cached ToME ``game`` singleton, refreshing it when stale."""
        if not self.attached:
            return None

        if self._game_table:
            if not force_validate and self._game_table_reads_until_validate > 0:
                self._game_table_reads_until_validate -= 1
                return self._game_table
            if _validate_game_table(self._handle, self._game_table):
                self._game_table_reads_until_validate = _GAME_TABLE_REVALIDATE_INTERVAL
                return self._game_table
            self._game_table = 0
            self._game_table_reads_until_validate = 0
            self._player_table = 0

        h = self._handle
        game_tab = _tab_get_table(h, self._global_table, "game")
        if game_tab is None or not _validate_game_table(h, game_tab):
            self._game_table = 0
            self._game_table_reads_until_validate = 0
            self._player_table = 0
            return None

        self._game_table = game_tab
        self._game_table_reads_until_validate = _GAME_TABLE_REVALIDATE_INTERVAL
        return game_tab

    def _read_player_stat_identifier(self, h: int, player_tab: int) -> str | None:
        """Return ToME's profile bucket id for the current character."""
        descriptor_tab = _tab_get_table(h, player_tab, "descriptor")
        if descriptor_tab is None:
            return None

        parts: list[str] = []
        for key in ("world", "subrace", "subclass", "difficulty", "permadeath"):
            value = _tab_get_string(h, descriptor_tab, key)
            if not value:
                return None
            parts.append(value)
        return ",".join(parts)

    def _read_player_base_stats(self, h: int, player_tab: int) -> dict[str, float]:
        """Return base stats from ``game.player.stats``.

        The live table has a leading nil array slot before the actual stat
        entries, so indexed reads are offset by one here on purpose. String-key
        lookups are kept as a fallback for older or unmigrated saves.
        """
        stats_tab = _tab_get_table(h, player_tab, "stats")
        if stats_tab is None:
            return {}

        values: dict[str, float] = {}
        for stat_index, short_name in enumerate(_PLAYER_BASE_STAT_ORDER, start=1):
            value = _tab_get_number(h, stats_tab, short_name)
            if value is None:
                value = _tab_get_number_by_index(h, stats_tab, stat_index + 1)
            values[short_name] = float(value) if isinstance(value, (int, float)) else 0.0
        return values

    def _read_player_total_stats(self, h: int, player_tab: int) -> dict[str, float]:
        """Return effective stats as ToME checks them for talent requirements."""
        totals = self._read_player_base_stats(h, player_tab)
        inc_stats_tab = _tab_get_table(h, player_tab, "inc_stats")
        if inc_stats_tab is None:
            return totals

        for stat_index, short_name in enumerate(_PLAYER_BASE_STAT_ORDER, start=1):
            bonus = _tab_get_number(h, inc_stats_tab, short_name)
            if bonus is None:
                bonus = _tab_get_number_by_index(h, inc_stats_tab, stat_index + 1)
            if isinstance(bonus, (int, float)):
                totals[short_name] = totals.get(short_name, 0.0) + float(bonus)
        return totals

    def read_player_hp(self) -> tuple[float, float] | None:
        """Return (life, max_life) or None if unavailable."""
        player_tab = self._ensure_player_table()
        if player_tab is None:
            return None

        h = self._handle
        life = _tab_get_number(h, player_tab, "life")
        max_life = _tab_get_number(h, player_tab, "max_life")
        if life is None or max_life is None:
            self._player_table = 0
            player_tab = self._ensure_player_table()
            if player_tab is None:
                return None
            life = _tab_get_number(h, player_tab, "life")
            max_life = _tab_get_number(h, player_tab, "max_life")
        if life is None or max_life is None:
            return None
        return life, max_life

    def read_player_mana(self) -> tuple[float, float] | None:
        """Return (mana, max_mana) or None if character has no mana."""
        pt = self._ensure_player_table()
        if pt is None:
            return None
        h = self._handle
        mana = _tab_get_number(h, pt, "mana")
        max_mana = _tab_get_number(h, pt, "max_mana")
        if mana is None or max_mana is None or max_mana <= 0:
            return None
        return mana, max_mana

    def read_level_id(self) -> str | None:
        """Return game.level.id string, or None."""
        if not self.attached:
            return None
        h = self._handle

        game_tab = self._ensure_game_table()
        if game_tab is None:
            return None
        level_tab = _tab_get_table(h, game_tab, "level")
        if level_tab is None:
            return None
        return _tab_get_string(h, level_tab, "id")

    def read_player_stats(self) -> PlayerStats | None:
        """Read the player's combat-relevant stats for danger comparison."""
        pt = self._ensure_player_table()
        if pt is None:
            return None

        h = self._handle

        level = _tab_get_number(h, pt, "level")
        if level is None:
            return None

        def _dump_numeric_subtable(sub_key: str) -> dict[str, float]:
            sub_ptr = _tab_get_table(h, pt, sub_key)
            if not sub_ptr:
                return {}
            raw = _tab_dump_flat(h, sub_ptr)
            return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}

        raw_resists = _dump_numeric_subtable("resists")
        raw_caps = _dump_numeric_subtable("resists_cap")

        return PlayerStats(
            level=level,
            max_life=_tab_get_number(h, pt, "max_life") or 0.0,
            armor=_tab_get_number(h, pt, "combat_armor") or 0.0,
            defense=_tab_get_number(h, pt, "combat_def") or 0.0,
            phys_save=_tab_get_number(h, pt, "combat_physresist") or 0.0,
            spell_save=_tab_get_number(h, pt, "combat_spellresist") or 0.0,
            mental_save=_tab_get_number(h, pt, "combat_mentalresist") or 0.0,
            die_at=_tab_get_number(h, pt, "die_at") or 0.0,
            armor_hardiness=_tab_get_number(h, pt, "combat_armor_hardiness") or 0.0,
            evasion=_tab_get_number(h, pt, "evasion") or 0.0,
            ignore_direct_crits=_tab_get_number(h, pt, "ignore_direct_crits") or 0.0,
            resists=_effective_player_resists(raw_resists, raw_caps),
            resists_pen=_dump_numeric_subtable("resists_pen"),
            resists_cap=raw_caps,
        )

    def read_player_sprite(self) -> tuple[str, list[str]] | None:
        """Return (image, sprite_layers) for the live player actor, or None."""
        pt = self._ensure_player_table()
        if pt is None:
            return None

        h = self._handle
        all_fields = _tab_dump_all(h, pt)
        image, sprite_layers = self._extract_actor_sprite(h, pt, all_fields)
        if not image and not sprite_layers:
            return None
        return image, sprite_layers

    def _normalize_bucket_slot(self, bucket_name: str, item_index: int) -> str:
        if bucket_name == "FINGER":
            return "Left ring" if item_index == 0 else "Right ring"
        return _INVENTORY_BUCKET_SLOT_LABELS.get(bucket_name, bucket_name.title())

    def _extract_item_entry(
        self,
        h: int,
        item_ptr: int,
        *,
        bucket_name: str,
        item_index: int,
    ) -> dict[str, Any]:
        flat = _tab_dump_flat(h, item_ptr)
        name = str(flat.get("name") or "Unknown Item")
        item_type = str(flat.get("type") or "")
        subtype = str(flat.get("subtype") or "")
        slot = self._normalize_bucket_slot(bucket_name, item_index)

        entry: dict[str, Any] = {
            "Name": name,
            "Slot": slot,
        }
        if item_type:
            entry["Type"] = item_type
        if subtype:
            entry["Subtype"] = subtype
        for src_key, label in (
            ("short_name", "ShortName"),
            ("define_as", "DefineAs"),
            ("moddable_tile", "ModdableTile"),
        ):
            value = flat.get(src_key)
            if isinstance(value, str) and value:
                entry[label] = value

        material_level = flat.get("material_level")
        if isinstance(material_level, (int, float)):
            entry["MaterialLevel"] = int(material_level)
            entry["Tier"] = int(material_level)

        encumber = flat.get("encumber")
        if isinstance(encumber, (int, float)):
            entry["Encumbrance"] = float(encumber)

        tags: list[str] = []
        if flat.get("identified") is True:
            tags.append("identified")
        if flat.get("unique"):
            tags.append("unique")
        if flat.get("__transmo") is True:
            tags.append("transmo")
        if tags:
            entry["Tags"] = tags

        properties: dict[str, str] = {}
        for src_key, label in (
            ("desc", "Description"),
            ("power_source", "Power source"),
            ("unided_name", "Unidentified name"),
        ):
            value = flat.get(src_key)
            if isinstance(value, str) and value:
                properties[label] = " ".join(value.split())
        if properties:
            entry["Properties"] = properties
        if isinstance(flat.get("image"), str) and str(flat["image"]).endswith(".png"):
            entry["Icon"] = str(flat["image"])

        return entry

    def _tab_get_named_child_table(self, h: int, tab_ptr: int, key: str) -> int | None:
        node = _tab_find_strkey(h, tab_ptr, key)
        if node is None or _ru32(h, node + 4) != _LJ_TTAB:
            return None
        child_ptr = _ru32(h, node)
        return child_ptr if (child_ptr and _is_heap(child_ptr)) else None

    def _read_talent_definition(
        self,
        h: int,
        talent_ptr: int,
        *,
        level: float,
        points_cap: float,
    ) -> dict[str, Any]:
        flat = _tab_dump_flat(h, talent_ptr)
        entry: dict[str, Any] = {
            "Level": f"{int(level)}/{max(int(points_cap), 1)}",
        }
        for src_key, label in (
            ("range", "Range"),
            ("cooldown", "Cooldown"),
        ):
            value = flat.get(src_key)
            if isinstance(value, (int, float)):
                entry[label] = str(int(value) if float(value).is_integer() else value)

        if isinstance(flat.get("mode"), str):
            entry["Mode"] = str(flat["mode"])
        if isinstance(flat.get("image"), str) and str(flat["image"]).endswith(".png"):
            entry["Icon"] = str(flat["image"])
        return entry

    @staticmethod
    def _category_key(display_name: str) -> str:
        """Keep category labels visually unchanged while never colliding with talent names."""
        return f"{display_name}\u200b"

    @staticmethod
    def _display_category_name(type_key: str, raw_name: str) -> str:
        def cap_first(text: str) -> str:
            text = " ".join(text.split()).strip()
            if not text:
                return ""
            return text[:1].upper() + text[1:]

        category = cap_first(type_key.split("/", 1)[0])
        name = cap_first(raw_name)
        if category and name:
            return f"{category} / {name}"
        return name or category or type_key

    def read_player_talents(self) -> dict[str, dict[str, Any]]:
        """Return live talent sections compatible with CharacterSheetView."""
        pt = self._ensure_player_table()
        if pt is None:
            return {}

        h = self._handle
        gt = self._global_table
        engine_tab = _tab_get_table(h, gt, "engine")
        interface_tab = _tab_get_table(h, engine_tab, "interface") if engine_tab else None
        actor_talents_tab = _tab_get_table(h, interface_tab, "ActorTalents") if interface_tab else None
        talents_tab = _tab_get_table(h, pt, "talents")
        talent_types_tab = _tab_get_table(h, pt, "talents_types")
        mastery_tab = _tab_get_table(h, pt, "talents_types_mastery")
        type_defs_tab = _tab_get_table(h, actor_talents_tab, "talents_types_def") if actor_talents_tab else None
        if None in (talents_tab, talent_types_tab, mastery_tab, type_defs_tab):
            return {}

        learned_levels = _tab_dump_all(h, talents_tab)
        enabled_types = _tab_dump_all(h, talent_types_tab)
        mastery_values = _tab_dump_all(h, mastery_tab)

        ordered_sections: dict[str, list[tuple[bool, list[tuple[str, Any]]]]] = {
            "Class Talents": [],
            "Generic Talents": [],
        }

        for type_ptr in _tab_get_ordered_tables(h, type_defs_tab):
            type_flat = _tab_dump_flat(h, type_ptr)
            type_key = str(type_flat.get("type") or "")
            if not type_key:
                continue
            if type_flat.get("hide"):
                continue
            if enabled_types.get(type_key) is None:
                continue

            type_name = self._display_category_name(type_key, str(type_flat.get("name") or ""))
            mastery = mastery_values.get(type_key)
            mastery_text = f"{float(mastery) + 1.0:.2f}" if isinstance(mastery, (int, float)) else "1.00"
            talents_list_tab = _tab_get_table(h, type_ptr, "talents")
            if talents_list_tab is None:
                continue
            talent_ptrs = _tab_get_ordered_tables(h, talents_list_tab)
            if not talent_ptrs:
                continue

            section_name = "Generic Talents" if type_flat.get("generic") is True else "Class Talents"
            category_entries: list[tuple[str, Any]] = [(self._category_key(type_name), mastery_text)]

            for talent_ptr in talent_ptrs:
                talent_flat = _tab_dump_flat(h, talent_ptr)
                if talent_flat.get("hide"):
                    continue
                talent_id = str(talent_flat.get("id") or "")
                talent_name = str(talent_flat.get("name") or talent_id)
                if not talent_name:
                    continue
                current_level = learned_levels.get(talent_id, 0.0)
                if not isinstance(current_level, (int, float)):
                    current_level = 0.0
                points_cap = talent_flat.get("points")
                if not isinstance(points_cap, (int, float)):
                    points_cap = 5.0
                category_entries.append(
                    (
                        talent_name,
                        self._read_talent_definition(
                            h,
                            talent_ptr,
                            level=float(current_level),
                            points_cap=float(points_cap),
                        ),
                    )
                )

            is_known = enabled_types.get(type_key) is True
            ordered_sections[section_name].append((is_known, category_entries))

        sections: dict[str, dict[str, Any]] = {}
        for section_name, groups in ordered_sections.items():
            if not groups:
                continue
            section: dict[str, Any] = {}
            for is_known in (True, False):
                for group_known, entries in groups:
                    if group_known is not is_known:
                        continue
                    for entry_name, entry_data in entries:
                        section[entry_name] = entry_data
            if section:
                sections[section_name] = section
        return sections

    def read_sustain_talents(self) -> dict[str, dict[str, Any]]:
        """Return the player's currently active sustained talents."""
        pt = self._ensure_player_table()
        if pt is None:
            return {}

        h = self._handle
        sustains_tab = _tab_get_table(h, pt, "sustain_talents")
        if sustains_tab is None:
            return {}
        talents_tab = _tab_get_table(h, pt, "talents")

        entries: list[tuple[str, dict[str, Any]]] = []
        for talent_id, value_it, value_lo in _tab_iter_string_entries(h, sustains_tab):
            if not talent_id.startswith("T_") or value_it not in (_LJ_TTRUE, _LJ_TTAB):
                continue

            record = lookup_talent_by_id(talent_id)
            name = _display_engine_id(talent_id, "T_")
            level = _tab_get_number(h, talents_tab, talent_id) if talents_tab else None
            entry: dict[str, Any] = {
                "Level": _format_live_number(level) if level is not None else "On",
                "Mode": record.mode.title() if record and record.mode else "Sustained",
                "Status": "Active",
            }
            if record and record.talent_type:
                entry["Type"] = record.talent_type
            if record and record.description:
                entry["Description"] = record.description
            if record and record.icon:
                entry["Icon"] = record.icon

            if value_it == _LJ_TTAB and _is_heap(value_lo):
                sustain_state = _tab_dump_flat(h, value_lo)
                for key, label in (("power", "Power"), ("charges", "Charges"), ("stacks", "Stacks")):
                    raw = sustain_state.get(key)
                    if isinstance(raw, (int, float)):
                        entry[label] = _format_live_number(float(raw))

            entries.append((name, entry))

        entries.sort(key=lambda item: item[0].lower())
        return {name: entry for name, entry in entries}

    def read_player_effects(self) -> dict[str, dict[str, Any]]:
        """Return the player's active timed effects from ``player.tmp``."""
        pt = self._ensure_player_table()
        if pt is None:
            return {}

        h = self._handle
        tmp_tab = _tab_get_table(h, pt, "tmp")
        if tmp_tab is None:
            return {}

        entries: list[tuple[int, str, str, dict[str, Any]]] = []
        for effect_id, value_it, value_lo in _tab_iter_string_entries(h, tmp_tab):
            if not effect_id.startswith("EFF_") or value_it != _LJ_TTAB or not _is_heap(value_lo):
                continue

            effect_state = _tab_dump_flat(h, value_lo)
            record = lookup_effect_by_id(effect_id)
            name = record.name if record and record.name else _display_engine_id(effect_id, "EFF_")
            entry: dict[str, Any] = {
                "Level": "On",
            }
            if record and record.status:
                entry["Status"] = record.status.title()
            if record and record.effect_type:
                entry["Type"] = record.effect_type.title()
            if record and record.icon:
                entry["Icon"] = record.icon

            duration = effect_state.get("dur")
            if isinstance(duration, (int, float)) and (record is None or record.decrease > 0):
                turns_left = float(duration) + 1.0
                turns_text = _format_live_number(turns_left)
                entry["Level"] = f"{turns_text}t"
                entry["Turn Duration"] = turns_text

            for key, label in (("charges", "Charges"), ("power", "Power"), ("stacks", "Stacks")):
                raw = effect_state.get(key)
                if isinstance(raw, (int, float)):
                    entry[label] = _format_live_number(float(raw))

            source_name = effect_state.get("srcname")
            if not isinstance(source_name, str) or not source_name:
                src_tab = self._tab_get_named_child_table(h, value_lo, "src")
                if src_tab is not None:
                    source_name = _tab_get_string(h, src_tab, "name")
            if isinstance(source_name, str) and source_name:
                entry["Source"] = source_name

            description = ""
            if record:
                description = record.summary or record.description
            elif isinstance(effect_state.get("desc"), str):
                description = str(effect_state["desc"])
            if description:
                entry["Description"] = description

            status_sort = 0 if str(entry.get("Status", "")).lower() == "beneficial" else 1
            entries.append((status_sort, name.lower(), name, entry))

        entries.sort(key=lambda item: (item[0], item[1]))
        return {name: entry for _, _, name, entry in entries}

    def read_player_inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (equipped_items, current_inventory_items, transmog_items) from game.player.inven."""
        pt = self._ensure_player_table()
        if pt is None:
            return [], [], []

        h = self._handle
        inven_tab = _tab_get_table(h, pt, "inven")
        if inven_tab is None:
            return [], [], []

        equipped: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        transmog: list[dict[str, Any]] = []

        for bucket_ptr in _tab_get_ordered_tables(h, inven_tab):
            bucket = _tab_dump_flat(h, bucket_ptr)
            bucket_name = str(bucket.get("name") or bucket.get("short_name") or "")
            if not bucket_name:
                continue
            item_ptrs = _tab_get_ordered_tables(h, bucket_ptr)
            if not item_ptrs:
                continue
            worn = bucket.get("worn") is True
            for idx, item_ptr in enumerate(item_ptrs):
                flat = _tab_dump_flat(h, item_ptr)
                if str(flat.get("define_as") or "") == "TRANSMO_CHEST":
                    continue

                entry = self._extract_item_entry(
                    h,
                    item_ptr,
                    bucket_name=bucket_name,
                    item_index=idx,
                )
                if worn:
                    equipped.append(entry)
                    continue

                if flat.get("__transmo") is True:
                    transmog.append(entry)
                    continue

                current.append(entry)

        return equipped, current, transmog

    def _extract_actor_sprite(
        self,
        h: int,
        actor_ptr: int,
        all_fields: dict[str, str | float | bool],
    ) -> tuple[str, list[str]]:
        """Resolve the representative image and ordered sprite layers for one actor.

        Memory walk (``add_mos`` layers) lives here; the "is this a usable
        sprite path?" predicate and final-choice logic live in
        :mod:`gui.sprite_resolve` so they're testable without a game process.
        """
        sprite_layers: list[str] = []
        base_layer_image = ""
        add_mos_tab = _tab_get_table(h, actor_ptr, "add_mos")
        if add_mos_tab:
            for sub_ptr in _tab_get_ordered_tables(h, add_mos_tab):
                sub = _tab_dump_flat(h, sub_ptr)
                img = str(sub.get("image") or "")
                if not is_usable_sprite(img):
                    continue
                sprite_layers.append(img)
                if not base_layer_image and sub.get("is_inate") == "base":
                    base_layer_image = img

        actor_image = pick_actor_image(
            raw_image=str(all_fields.get("image") or ""),
            sprite_layers=sprite_layers,
            base_layer_image=base_layer_image,
            attachement_spots=str(all_fields.get("attachement_spots") or ""),
        )
        return actor_image, sprite_layers

    def read_entities(self, min_rank: float = 1.5) -> list[EntityInfo]:
        """
        Read all actors from game.level.entities with rank > min_rank.
        Excludes dead actors and the player.  Computes a danger rating
        relative to the current player.  Returns a list sorted by danger
        score (most dangerous first).
        """
        if not self.attached:
            return []
        h = self._handle

        game_tab = self._ensure_game_table()
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

            # Keep the hot path lean: only snapshot the fields the panel uses.
            all_fields = _tab_dump_entity_snapshot(h, ptr)

            name = (all_fields.get("name") or "?") if isinstance(all_fields.get("name"), str) else "?"
            life = float(all_fields.get("life") or 0.0)
            max_life = float(all_fields.get("max_life") or 0.0)
            level = float(all_fields.get("level") or 0.0)
            faction = (all_fields.get("faction") or "?") if isinstance(all_fields.get("faction"), str) else "?"
            type_name = (all_fields.get("type") or "") if isinstance(all_fields.get("type"), str) else ""
            subtype = (all_fields.get("subtype") or "") if isinstance(all_fields.get("subtype"), str) else ""
            size_cat = float(all_fields.get("size_category") or 0.0)
            unique = bool(all_fields.get("unique", False))

            # ── Image resolution ──────────────────────────────────────────
            # Four patterns in ToME:
            #   Direct:            image = "npc/troll_f.png"  → use directly.
            #   nice_tile:         image = "invis.png"; real path in add_mos entries.
            #   attachement_spots: string field holding "npc/xxx.png" (random bosses).
            #   composite (golem): image = "player/…shadow…"; add_mos has ordered
            #                      integer-keyed layer entries for full compositing.
            entity_image, sprite_layers = self._extract_actor_sprite(h, ptr, all_fields)

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
                sprite_layers=sprite_layers,
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
        pt = self._ensure_player_table()
        if pt is None:
            return {}

        h = self._handle

        keys = [
            "life",
            "max_life",
            "mana",
            "max_mana",
            "stamina",
            "max_stamina",
            "vim",
            "max_vim",
            "positive",
            "max_positive",
            "negative",
            "max_negative",
            "psi",
            "max_psi",
            "hate",
            "max_hate",
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

    def read_player_exp(self) -> tuple[float, float] | None:
        """Return ``(current_total_exp, next_level_total_exp)`` or None."""
        pt = self._ensure_player_table()
        if pt is None:
            return None
        h = self._handle
        exp = _tab_get_number(h, pt, "exp")
        level = _tab_get_number(h, pt, "level")
        if exp is None or level is None:
            return None
        exp_mod = _tab_get_number(h, pt, "exp_mod") or 1.0
        needed = _tome_exp_chart(int(level) + 1, exp_mod)
        return exp, needed

    def read_has_transmo(self) -> bool:
        """Return True if the player has the transmogrification chest unlocked."""
        pt = self._ensure_player_table()
        if pt is None:
            return True  # default to True when memory unavailable
        val = _tab_get_number(self._handle, pt, "has_transmo")
        return val is not None and val > 0

    def read_visited_zones(self) -> set[str]:
        """Return the set of zone short_names the player has visited."""
        if not self.attached:
            return set()
        h = self._handle
        game_tab = self._ensure_game_table()
        if game_tab is None:
            return set()
        visited_tab = _tab_get_table(h, game_tab, "visited_zones")
        if visited_tab is None:
            return set()
        return _tab_string_keys_with_true(h, visited_tab, max_key_len=128)

    def read_unique_deaths(self) -> set[str]:
        """Return the set of unique entity names recorded in ``game.state.unique_death``.

        These are boss/unique kills — ToME only sets the flag to ``true`` on
        death, so this is a useful "which named enemies have I killed" list.
        """
        if not self.attached:
            return set()
        h = self._handle
        game_tab = self._ensure_game_table()
        if game_tab is None:
            return set()
        state_tab = _tab_get_table(h, game_tab, "state")
        if state_tab is None:
            return set()
        deaths_tab = _tab_get_table(h, state_tab, "unique_death")
        if deaths_tab is None:
            return set()
        return _tab_string_keys_with_true(h, deaths_tab, max_key_len=256)

    def read_unique_encounters(self) -> set[str]:
        """Return names tracked in ``game.uniques`` (seen or killed uniques)."""
        pt = self._ensure_player_table()
        if pt is None:
            return set()
        h = self._handle
        game_tab = self._ensure_game_table()
        if game_tab is None:
            return set()
        uniques_tab = _tab_get_table(h, game_tab, "uniques")
        if uniques_tab is None:
            return set()

        cid = self._read_player_stat_identifier(h, pt)
        if not cid:
            return set()
        bucket_tab = _tab_get_table(h, uniques_tab, cid)
        if bucket_tab is None:
            return set()

        flat = _tab_dump_flat(h, bucket_tab)
        return {
            key
            for key, value in flat.items()
            if value is True or (isinstance(value, (int, float)) and float(value) > 0.0)
        }

    def read_current_zone(self) -> tuple[str, int, int] | None:
        """Return (short_name, current_floor, max_floors) or None."""
        if not self.attached:
            return None
        h = self._handle
        game_tab = self._ensure_game_table()
        if game_tab is None:
            return None
        zone_tab = _tab_get_table(h, game_tab, "zone")
        if zone_tab is None:
            return None
        short_name = _tab_get_string(h, zone_tab, "short_name")
        max_level = _tab_get_number(h, zone_tab, "max_level")
        if short_name is None or max_level is None:
            return None
        level_tab = _tab_get_table(h, game_tab, "level")
        if level_tab is None:
            return None
        floor = _tab_get_number(h, level_tab, "level")
        if floor is None:
            return None
        return (short_name, int(floor), int(max_level))

    def _quest_matches_status(
        self,
        h: int,
        pt: int,
        quest_id: str,
        status_name: str,
        sub_key: str | None,
    ) -> bool | None:
        quests_tab = _tab_get_table(h, pt, "quests")
        if quests_tab is None:
            return None
        quest_tab = _tab_get_table(h, quests_tab, quest_id)
        if quest_tab is None:
            return False

        status_value = {
            "COMPLETED": _QUEST_COMPLETED,
            "DONE": _QUEST_DONE,
            "FAILED": _QUEST_FAILED,
        }.get(status_name)
        if status_value is None:
            return None

        if sub_key:
            objectives_tab = _tab_get_table(h, quest_tab, "objectives")
            if objectives_tab is None:
                return False
            objective = _tab_get_number(h, objectives_tab, sub_key)
            return bool(isinstance(objective, (int, float)) and int(objective) == status_value)

        status = _tab_get_number(h, quest_tab, "status")
        return bool(isinstance(status, (int, float)) and int(status) == status_value)

    def _read_allow_build_flags(self, h: int) -> dict[str, bool]:
        profile_tab = _tab_get_table(h, self._global_table, "profile")
        if profile_tab is None:
            return {}
        mod_tab = _tab_get_table(h, profile_tab, "mod")
        if mod_tab is None:
            return {}
        allow_build_tab = _tab_get_table(h, mod_tab, "allow_build")
        if allow_build_tab is None:
            return {}
        raw = _tab_dump_all(h, allow_build_tab)
        return {str(key): bool(value) for key, value in raw.items() if isinstance(key, str)}

    def _evaluate_prodigy_logic(
        self,
        h: int,
        pt: int,
        talents_tab: int | None,
        record: Any,
    ) -> tuple[bool, list[str]]:
        unmet: list[str] = []
        for description, block in zip(record.remaining_requirements, record.special_logic, strict=False):
            if description and not self._special_requirement_met(h, pt, talents_tab, block):
                unmet.append(description)
        return (not unmet, unmet)

    def _special_requirement_met(self, h: int, pt: int, talents_tab: int | None, block: str) -> bool:
        text = " ".join(block.split())

        if any(
            marker in text
            for marker in (
                "findInAllInventoriesBy(",
                "knownLore(",
                "is_necromancy",
                "supports_fallen_transform",
                "supports_lich_transform",
                "lichform_quest_checker",
            )
        ):
            return False

        if "self.damage_log" in text or "self.damage_intake_log" in text:
            log_name = "damage_intake_log" if "self.damage_intake_log" in text else "damage_log"
            log_tab = _tab_get_table(h, pt, log_name)
            if log_tab is None:
                return False

            threshold_match = _RE_REQ_GE.search(text)
            threshold = int(threshold_match.group(1)) if threshold_match else 0

            if "self.damage_log.weapon." in text:
                weapon_tab = _tab_get_table(h, log_tab, "weapon")
                if weapon_tab is None:
                    return False
                matched_keys = [
                    weapon_key
                    for weapon_key in ("archery", "dualwield", "twohanded", "unarmed", "shield", "other")
                    if f"self.damage_log.weapon.{weapon_key}" in text
                ]
                if matched_keys:
                    return any(
                        isinstance(_tab_get_number(h, weapon_tab, weapon_key), (int, float))
                        and float(_tab_get_number(h, weapon_tab, weapon_key) or 0.0) >= threshold
                        for weapon_key in matched_keys
                    )
                return False

            damage_types = [dtype.upper() for dtype in _RE_REQ_DAMAGE_TYPE.findall(text)]
            if damage_types:
                for damage_type in damage_types:
                    value = _table_number_by_damage_type(h, log_tab, damage_type)
                    if isinstance(value, (int, float)) and float(value) >= threshold:
                        return True
                return False

        if "self.talent_kind_log" in text:
            talent_kind_tab = _tab_get_table(h, pt, "talent_kind_log")
            if talent_kind_tab is None:
                return False
            checks = _RE_REQ_TALENT_KIND.findall(text)
            if checks and not all(
                isinstance(_tab_get_number(h, talent_kind_tab, kind), (int, float))
                and float(_tab_get_number(h, talent_kind_tab, kind) or 0.0) >= int(required)
                for kind, required in checks
            ):
                return False

        attr_checks = _RE_REQ_ATTR.findall(text)
        if attr_checks:
            for attr_name, raw_threshold in attr_checks:
                value = _tab_get_scalar(h, pt, attr_name)
                if raw_threshold:
                    if not (isinstance(value, (int, float)) and float(value) >= float(raw_threshold)):
                        return False
                elif not value:
                    return False

        for attr_name in _RE_REQ_ATTR_MUST_BE_FALSE.findall(text):
            if _tab_get_scalar(h, pt, attr_name):
                return False

        for attr_name in _RE_REQ_ATTR_MUST_BE_TRUE.findall(text):
            if not _tab_get_scalar(h, pt, attr_name):
                return False

        allow_build_checks = _RE_REQ_ALLOW_BUILD.findall(text)
        if allow_build_checks:
            allow_build = self._read_allow_build_flags(h)
            if " or " in text:
                if not any(allow_build.get(key) is True for key in allow_build_checks):
                    return False
            elif not all(allow_build.get(key) is True for key in allow_build_checks):
                return False

        if match := _RE_REQ_SIZE.search(text):
            size_value = _tab_get_number(h, pt, "size_category")
            if not (isinstance(size_value, (int, float)) and float(size_value) >= float(match.group(1))):
                return False

        if match := _RE_REQ_COMBAT_DEF.search(text):
            defense_value = _tab_get_number(h, pt, "combat_def")
            if not (isinstance(defense_value, (int, float)) and float(defense_value) >= float(match.group(1))):
                return False

        if match := _RE_REQ_LUCK.search(text):
            luck_value = _tab_get_number(h, pt, "lck")
            if luck_value is None:
                luck_value = self._read_player_total_stats(h, pt).get("lck")
            if not (isinstance(luck_value, (int, float)) and float(luck_value) >= float(match.group(1))):
                return False

        field_checks = _RE_REQ_SELF_FIELD.findall(text)
        for field_name, raw_required in field_checks:
            value = _tab_get_number(h, pt, field_name)
            if not (isinstance(value, (int, float)) and float(value) >= float(raw_required)):
                return False

        talent_checks = _RE_REQ_KNOW_TALENT.findall(text)
        if talent_checks:
            if talents_tab is None:
                return False
            levels = [
                _tab_get_number(h, talents_tab, talent_id)
                for talent_id in talent_checks
            ]
            if " or " in text:
                if not any(isinstance(level, (int, float)) and float(level) >= 1.0 for level in levels):
                    return False
            else:
                if not all(isinstance(level, (int, float)) and float(level) >= 1.0 for level in levels):
                    return False

        talent_level_checks = _RE_REQ_TALENT_RAW.findall(text)
        if talent_level_checks:
            if talents_tab is None:
                return False
            for talent_id, required in talent_level_checks:
                level = _tab_get_number(h, talents_tab, talent_id)
                if not (isinstance(level, (int, float)) and float(level) >= float(required)):
                    return False

        if "inscription_restrictions" in text or "inscription_forbids" in text:
            for table_name, key in _RE_REQ_INSCRIPTION.findall(text):
                tab = _tab_get_table(h, pt, f"inscription_{table_name}")
                value = _tab_get_bool(h, tab, key) if tab else None
                if table_name == "restrictions" and value is False:
                    return False
                if table_name == "forbids" and value is True:
                    return False

        if "hasQuest" in text or "isQuestStatus" in text:
            quest_ids = _RE_REQ_QUEST_ID.findall(text)
            if not quest_ids:
                return False
            quest_id = quest_ids[0]
            for negated, status_name, sub_key in _RE_REQ_QUEST_STATUS.findall(text):
                matched = self._quest_matches_status(h, pt, quest_id, status_name, sub_key or None)
                if matched is None or bool(negated.strip()) == bool(matched):
                    return False

            for negated, sub_key in _RE_REQ_QUEST_COMPLETED.findall(text):
                matched = self._quest_matches_status(h, pt, quest_id, "COMPLETED", sub_key)
                if matched is None or bool(negated.strip()) == bool(matched):
                    return False

        recognized = any(
            marker in text
            for marker in (
                "damage_log",
                "damage_intake_log",
                "talent_kind_log",
                ':attr("',
                "profile.mod.allow_build",
                "size_category",
                "getLck()",
                "knowTalent(",
                "getTalentLevelRaw(",
                "inscription_restrictions",
                "inscription_forbids",
                "hasQuest(",
                "isQuestStatus(",
            )
        )
        return recognized

    def read_prodigies(self) -> list[dict[str, Any]]:
        """Return prodigies currently eligible to appear in the live UI.

        A prodigy is shown only when the player is level 25+, satisfies every
        stat gate, has not already learned it, and knows any required talent
        categories. Remaining quest or talent-specific requirements are kept in
        the returned detail payload for the sheet's right-side info panel.
        """
        pt = self._ensure_player_table()
        if pt is None:
            return []

        h = self._handle

        # Level gate: prodigies unlock at 25
        level = _tab_get_number(h, pt, "level")
        if level is None or level < 25:
            return []

        player_stats = self._read_player_total_stats(h, pt)
        if not player_stats:
            return []

        # Read learned talents from game.player.talents
        talents_tab = _tab_get_table(h, pt, "talents")
        learned_ids: set[str] = set()
        if talents_tab is not None:
            talents_flat = _tab_dump_flat(h, talents_tab)
            learned_ids = {
                tid
                for tid in _get_prodigy_db()
                if isinstance(talents_flat.get(tid), (int, float)) and float(talents_flat[tid]) >= 1.0
            }

        talent_types_tab = _tab_get_table(h, pt, "talents_types")
        enabled_types = _tab_dump_all(h, talent_types_tab) if talent_types_tab is not None else {}
        descriptor_tab = _tab_get_table(h, pt, "descriptor")
        descriptor_values = _tab_dump_flat(h, descriptor_tab) if descriptor_tab is not None else {}

        available: list[dict[str, Any]] = []
        for tid, record in _get_prodigy_db().items():
            if tid in learned_ids:
                continue
            if any(
                player_stats.get(stat_key, 0.0) < float(required)
                for stat_key, required in record.stat_requirements
            ):
                continue
            if any(str(descriptor_values.get(key) or "") != expected for key, expected in record.birth_descriptors):
                continue
            if record.class_evolution_for and record.class_evolution_for not in {
                str(descriptor_values.get("class") or ""),
                str(descriptor_values.get("subclass") or ""),
            }:
                continue
            if record.race_evolution_logic and not self._special_requirement_met(h, pt, talents_tab, record.race_evolution_logic):
                continue
            if any(enabled_types.get(type_key) is not True for type_key, _desc in record.category_requirements):
                continue
            special_ok, unmet_requirements = self._evaluate_prodigy_logic(h, pt, talents_tab, record)
            if record.special_logic and not special_ok:
                continue

            entry: dict[str, Any] = {
                "Name": record.name,
                "Level": "0/1",
                "Effective Talent Level": "1.00",
                "Mode": record.mode.title() if record.mode else "Activated",
            }
            if record.talent_type:
                entry["Type"] = record.talent_type
            if record.icon:
                entry["Icon"] = record.icon
            entry["Remaining Requirements"] = _format_requirement_text(unmet_requirements)
            if record.description:
                entry["Description"] = record.description
            available.append(entry)

        return sorted(available, key=lambda item: str(item.get("Name") or "").lower())


# ── Background pre-attach ─────────────────────────────────────────────────────
# Lets app.py kick off the t-engine.exe scan + Lua _G locate at the earliest
# possible point during startup, in parallel with QApplication construction
# and the existing-instance shutdown wait.  The DashboardTab then adopts the
# pre-warmed reader instead of constructing a fresh one and waiting on a
# synchronous tasklist call.
_preattach_lock: _threading.Lock = _threading.Lock()
_preattach_event: _threading.Event = _threading.Event()
_preattached_reader: MemoryReader | None = None


def start_background_preattach() -> None:
    """Begin attaching to t-engine.exe in a background daemon thread.

    Idempotent — calling twice is a no-op.  The result is consumed once via
    :func:`take_preattached_reader`; until then the reader (whether attached
    successfully or not) is stashed at module level.
    """
    global _preattached_reader
    with _preattach_lock:
        if _preattached_reader is not None:
            return
        reader = MemoryReader()
        _preattached_reader = reader

    def _go() -> None:
        try:
            reader.attach()  # silent failure if game not running
        except Exception:  # noqa: BLE001
            pass
        finally:
            _preattach_event.set()

    _threading.Thread(target=_go, daemon=True, name="MemoryReader.preattach").start()


def take_preattached_reader(*, wait_timeout: float = 0.0) -> MemoryReader | None:
    """Take ownership of the pre-attached reader, if one was started AND its
    background attach has fully finished.

    Returns ``None`` if :func:`start_background_preattach` was never called,
    OR the bg attach thread is still in flight after the optional wait.  Not
    returning an in-flight reader is critical: otherwise a subsequent
    ``attach()`` call from the dashboard would race the bg thread on the same
    instance, clobbering each other's process handle mid-scan.
    """
    if wait_timeout > 0:
        completed = _preattach_event.wait(wait_timeout)
    else:
        completed = _preattach_event.is_set()
    if not completed:
        # Orphan the in-flight reader — it'll finish writing to itself and be
        # garbage-collected.  Dashboard will construct a fresh instance.
        return None
    global _preattached_reader
    with _preattach_lock:
        r = _preattached_reader
        _preattached_reader = None
    return r
