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
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from runtime_output import console_print

# ── Prodigy database ─────────────────────────────────────────────────────────
# Dynamically parsed from the game's tome.team zip archive at startup; falls
# back to a hardcoded snapshot if the archive cannot be found/parsed.
# Map: T_* ID → (display_name, primary_stat_key).
# Stat key matches game.player.stats sub-table: "str"|"dex"|"con"|"mag"|"wil"|"cun".
# All prodigies require level 25 and 50+ in the listed stat.

_UBER_STAT_FILES: Final[dict[str, str]] = {
    "data/talents/uber/str.lua": "str",
    "data/talents/uber/dex.lua": "dex",
    "data/talents/uber/const.lua": "con",
    "data/talents/uber/mag.lua": "mag",
    "data/talents/uber/wil.lua": "wil",
    "data/talents/uber/cun.lua": "cun",
}

# Regex to pull the body of each uberTalent{} block.
# We match from "uberTalent{" up to a lone "}" on its own line (the closing brace).
_RE_UBER_BLOCK = re.compile(
    r"uberTalent\s*\{(.*?)^\}",
    re.DOTALL | re.MULTILINE,
)
_RE_NAME = re.compile(r'\bname\s*=\s*"([^"]+)"')
_RE_SHORT_NAME = re.compile(r'\bshort_name\s*=\s*"([^"]+)"')
_RE_NOT_LISTED = re.compile(r"\bnot_listed\s*=\s*true\b")


def _find_tome_team() -> Path | None:
    """Locate the game's tome.team zip in common places."""
    import os

    candidates = [
        Path(os.environ.get("TEMP", "")) / "tome.team",
        Path(os.environ.get("TMP", "")) / "tome.team",
        Path(os.environ.get("LOCALAPPDATA", "")) / "T-Engine" / "tome.team",
        Path(os.environ.get("APPDATA", "")) / "T-Engine" / "tome.team",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _name_to_tid(name: str) -> str:
    """Convert a prodigy display name to its T_* ID (mirrors ToME's Lua logic)."""
    tid = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    return f"T_{tid}"


def _parse_prodigy_db(team_path: Path) -> dict[str, tuple[str, str]]:
    """Parse prodigy definitions from the game archive, skipping hidden ones."""
    db: dict[str, tuple[str, str]] = {}
    try:
        with zipfile.ZipFile(team_path, "r") as zf:
            names_in_zip = set(zf.namelist())
            for lua_path, stat_key in _UBER_STAT_FILES.items():
                if lua_path not in names_in_zip:
                    continue
                try:
                    src = zf.read(lua_path).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                for m in _RE_UBER_BLOCK.finditer(src):
                    body = m.group(1)
                    if _RE_NOT_LISTED.search(body):
                        continue
                    name_m = _RE_NAME.search(body)
                    if not name_m:
                        continue
                    display_name = name_m.group(1)
                    short_m = _RE_SHORT_NAME.search(body)
                    if short_m:
                        tid = f"T_{short_m.group(1).upper()}"
                    else:
                        tid = _name_to_tid(display_name)
                    db[tid] = (display_name, stat_key)
    except Exception:  # noqa: BLE001
        pass
    return db


def _build_prodigy_db() -> dict[str, tuple[str, str]]:
    """Return dynamic DB if parseable; otherwise fall back to static snapshot."""
    team_path = _find_tome_team()
    if team_path is not None:
        db = _parse_prodigy_db(team_path)
        if db:
            return db
    # ── Static fallback snapshot (ToME 1.7.x) ────────────────────────────────
    return {
        # Constitution
        "T_DRACONIC_BODY": ("Draconic Body", "con"),
        "T_BLOODSPRING": ("Bloodspring", "con"),
        "T_ETERNAL_GUARD": ("Eternal Guard", "con"),
        "T_NEVER_STOP_RUNNING": ("Never Stop Running", "con"),
        "T_ARMOUR_OF_SHADOWS": ("Armour of Shadows", "con"),
        "T_SPINE_OF_THE_WORLD": ("Spine of the World", "con"),
        "T_FUNGAL_BLOOD": ("Fungal Blood", "con"),
        "T_CORRUPTED_SHELL": ("Corrupted Shell", "con"),
        # Cunning  (T_FAST_AS_LIGHTNING excluded: not_listed=true)
        "T_TRICKY_DEFENSES": ("Tricky Defenses", "cun"),
        "T_ENDLESS_WOES": ("Endless Woes", "cun"),
        "T_SECRETS_OF_TELOS": ("Secrets of Telos", "cun"),
        "T_ELEMENTAL_SURGE": ("Elemental Surge", "cun"),
        "T_EYE_OF_THE_TIGER": ("Eye of the Tiger", "cun"),
        "T_WORLDLY_KNOWLEDGE": ("Worldly Knowledge", "cun"),
        "T_ADEPT": ("Adept", "cun"),
        "T_TRICKS_OF_THE_TRADE": ("Tricks of the Trade", "cun"),
        # Dexterity
        "T_FLEXIBLE_COMBAT": ("Flexible Combat", "dex"),
        "T_THROUGH_THE_CROWD": ("Through The Crowd", "dex"),
        "T_SWIFT_HANDS": ("Swift Hands", "dex"),
        "T_WINDBLADE": ("Windblade", "dex"),
        "T_WINDTOUCHED_SPEED": ("Windtouched Speed", "dex"),
        "T_CRAFTY_HANDS": ("Crafty Hands", "dex"),
        "T_ROLL_WITH_IT": ("Roll With It", "dex"),
        "T_VITAL_SHOT": ("Vital Shot", "dex"),
        # Magic  (T_SPECTRAL_SHIELD excluded: not_listed=true)
        "T_ETHEREAL_FORM": ("Ethereal Form", "mag"),
        "T_AETHER_PERMEATION": ("Aether Permeation", "mag"),
        "T_MYSTICAL_CUNNING": ("Mystical Cunning", "mag"),
        "T_ARCANE_MIGHT": ("Arcane Might", "mag"),
        "T_TEMPORAL_FORM": ("Temporal Form", "mag"),
        "T_BLIGHTED_SUMMONING": ("Blighted Summoning", "mag"),
        "T_REVISIONIST_HISTORY": ("Revisionist History", "mag"),
        "T_CAUTERIZE": ("Cauterize", "mag"),
        "T_LICH": ("Lich", "mag"),
        "T_HIGH_THAUMATURGIST": ("High Thaumaturgist", "mag"),
        # Strength
        "T_GIANT_LEAP": ("Giant Leap", "str"),
        "T_TITAN_S_SMASH": ("You Shall Be My Weapon!", "str"),
        "T_MASSIVE_BLOW": ("Massive Blow", "str"),
        "T_STEAMROLLER": ("Steamroller", "str"),
        "T_IRRESISTIBLE_SUN": ("Irresistible Sun", "str"),
        "T_NO_FATIGUE": ("I Can Carry The World!", "str"),
        "T_LEGACY_OF_THE_NALOREN": ("Legacy of the Naloren", "str"),
        "T_SUPERPOWER": ("Superpower", "str"),
        "T_AVATAR_OF_A_DISTANT_SUN": ("Avatar of a Distant Sun", "str"),
        # Willpower
        "T_DRACONIC_WILL": ("Draconic Will", "wil"),
        "T_METEORIC_CRASH": ("Meteoric Crash", "wil"),
        "T_GARKUL_S_REVENGE": ("Garkul's Revenge", "wil"),
        "T_HIDDEN_RESOURCES": ("Hidden Resources", "wil"),
        "T_LUCKY_DAY": ("Lucky Day", "wil"),
        "T_UNBREAKABLE_WILL": ("Unbreakable Will", "wil"),
        "T_SPELL_FEEDBACK": ("Spell Feedback", "wil"),
        "T_MENTAL_TYRANNY": ("Mental Tyranny", "wil"),
        "T_FALLEN": ("Fallen", "wil"),
    }


def _get_prodigy_db() -> dict[str, tuple[str, str]]:
    global _PRODIGY_DB
    if _PRODIGY_DB is None:
        _PRODIGY_DB = _build_prodigy_db()
    return _PRODIGY_DB


_PRODIGY_DB: dict[str, tuple[str, str]] | None = None

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
_NODE_SIZE = 24


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


# LuaJIT itype constants for bool values
_LJ_TTRUE = 0xFFFFFFFD
_LJ_TFALSE = 0xFFFFFFFE
_LJ_TNIL = 0xFFFFFFFF
_IMAGE_PREFIXES = ("npc/", "player/")


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
            key = prefix + key_raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

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
    "resists",  # damage type resistances  (keyed by DamageType int)
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
    if addr <= 0:
        return None
    game_tab = _tab_get_table(h, addr, "game")
    if game_tab is None:
        return None
    return addr


# ── Entity data ───────────────────────────────────────────────────────────────

# ToME rank values (numeric)
RANK_CRITTER = 1
RANK_NORMAL = 2
RANK_ELITE = 3
RANK_RARE = 3.2  # may vary
RANK_UNIQUE = 3.5
RANK_BOSS = 4
RANK_ELITE_BOSS = 5

RANK_NAMES: dict[int, str] = {
    1: "Critter",
    2: "Normal",
    3: "Elite",
    4: "Boss",
    5: "Elite Boss",
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

# Rank → weight for danger calculation (higher = scarier)
_RANK_WEIGHT: dict[int, float] = {
    1: 0.2,  # critter
    2: 1.0,  # normal
    3: 1.8,  # elite
    4: 3.0,  # boss
    5: 4.0,  # elite boss
}

DANGER_TRIVIAL = "Trivial"
DANGER_EASY = "Easy"
DANGER_MODERATE = "Moderate"
DANGER_DANGEROUS = "Dangerous"
DANGER_DEADLY = "Deadly"


def _rank_weight(rank: float) -> float:
    r = int(rank)
    if r in _RANK_WEIGHT:
        w = _RANK_WEIGHT[r]
    else:
        w = 1.0
    # Fractional ranks (3.2=rare, 3.5=unique) interpolate upward
    if rank >= 3.5:
        w = max(w, 2.4)  # unique
    elif rank >= 3.2:
        w = max(w, 2.0)  # rare
    return w


def compute_danger(enemy: EntityInfo, player: PlayerStats | None) -> tuple[str, float]:
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
    level_factor = level_delta / 5.0  # +5 levels = +1.0, -5 = -1.0

    # HP ratio: how tanky is the enemy compared to you
    hp_ratio = (enemy.max_life / player.max_life) if player.max_life > 0 else 1.0
    hp_factor = min(hp_ratio, 5.0) / 2.0  # cap at 5x, normalise ~0..2.5

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
    raw = rw * (1.0 + 0.4 * level_factor + 0.2 * hp_factor + 0.1 * save_delta + 0.1 * def_delta)

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
    danger: str  # label: Trivial / Easy / Moderate / Dangerous / Deadly
    danger_score: float  # numeric score for sorting
    image: str  # representative single sprite path, or ""
    sprite_layers: list[str]  # ordered add_mos layer paths for compositing (may be empty)
    # Extended fields
    type_name: str  # e.g. "insect"
    subtype: str  # e.g. "ant"
    size_category: float  # 1=tiny … 5=huge
    unique: bool  # True if a named/random unique
    # Full flat field dump (strings, numbers, bools) — for debug / tooltip
    all_fields: dict[str, str | float | bool]


# ── Public API ────────────────────────────────────────────────────────────────


class MemoryReader:
    """Reads live game state from t-engine.exe via ReadProcessMemory."""

    def __init__(self) -> None:
        self._handle: int = 0
        self._pid: int = 0
        self._global_table: int = 0  # _G GCtab address
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
                    self._player_table = 0
                    return True

        gt = _find_global_table(h)
        if gt is None:
            print("[memory] OpenProcess succeeded, but the Lua global table scan found no match.", file=sys.stderr)
            self.detach()
            return False

        self._global_table = gt
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
        self._player_table = 0

    def is_process_alive(self) -> bool:
        if not self._pid:
            return False
        return _get_pid("t-engine.exe") == self._pid

    def read_player_hp(self) -> tuple[float, float] | None:
        """Return (life, max_life) or None if unavailable."""
        if not self.attached:
            return None

        h = self._handle
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

        life = _tab_get_number(h, player_tab, "life")
        max_life = _tab_get_number(h, player_tab, "max_life")
        if life is None or max_life is None:
            return None
        return life, max_life

    def read_player_mana(self) -> tuple[float, float] | None:
        """Return (mana, max_mana) or None if character has no mana."""
        if not self._player_table or not self.attached:
            return None
        h = self._handle
        pt = self._player_table
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

        h = self._handle
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

    def read_player_sprite(self) -> tuple[str, list[str]] | None:
        """Return (image, sprite_layers) for the live player actor, or None."""
        if not self.attached:
            return None
        if not self._player_table:
            self.read_player_hp()
        if not self._player_table:
            return None

        h = self._handle
        pt = self._player_table
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
        if not self.attached:
            return {}
        if not self._player_table:
            self.read_player_hp()
        if not self._player_table:
            return {}

        h = self._handle
        gt = self._global_table
        engine_tab = _tab_get_table(h, gt, "engine")
        interface_tab = _tab_get_table(h, engine_tab, "interface") if engine_tab else None
        actor_talents_tab = _tab_get_table(h, interface_tab, "ActorTalents") if interface_tab else None
        talents_tab = _tab_get_table(h, self._player_table, "talents")
        talent_types_tab = _tab_get_table(h, self._player_table, "talents_types")
        mastery_tab = _tab_get_table(h, self._player_table, "talents_types_mastery")
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

    def read_player_inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (equipped_items, current_inventory_items, transmog_items) from game.player.inven."""
        if not self.attached:
            return [], [], []
        if not self._player_table:
            self.read_player_hp()
        if not self._player_table:
            return [], [], []

        h = self._handle
        inven_tab = _tab_get_table(h, self._player_table, "inven")
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
        """Resolve the representative image and ordered sprite layers for one actor."""
        raw_image = str(all_fields.get("image") or "")
        raw_usable = (
            any(raw_image.startswith(prefix) for prefix in _IMAGE_PREFIXES)
            and raw_image != "invis.png"
            and "shadow" not in raw_image
        )

        actor_image = raw_image if raw_usable else ""
        sprite_layers: list[str] = []

        add_mos_tab = _tab_get_table(h, actor_ptr, "add_mos")
        if add_mos_tab:
            base_img = ""
            for sub_ptr in _tab_get_ordered_tables(h, add_mos_tab):
                sub = _tab_dump_flat(h, sub_ptr)
                img = str(sub.get("image") or "")
                if not any(img.startswith(prefix) for prefix in _IMAGE_PREFIXES):
                    continue
                if "shadow" in img:
                    continue
                sprite_layers.append(img)
                if not base_img and sub.get("is_inate") == "base":
                    base_img = img
            if not actor_image:
                actor_image = base_img or (sprite_layers[0] if sprite_layers else "")

        if not actor_image:
            attach = str(all_fields.get("attachement_spots") or "")
            if any(attach.startswith(prefix) for prefix in _IMAGE_PREFIXES):
                actor_image = attach

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
        if not self._player_table or not self.attached:
            # Trigger a player table resolve
            self.read_player_hp()

        if not self._player_table:
            return {}

        h = self._handle
        pt = self._player_table

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
        """Return (exp_this_level, exp_needed) or None if unavailable."""
        if not self._player_table or not self.attached:
            self.read_player_hp()
        if not self._player_table:
            return None
        h = self._handle
        pt = self._player_table
        exp = _tab_get_number(h, pt, "exp")
        level = _tab_get_number(h, pt, "level")
        if exp is None or level is None:
            return None
        next_level = int(level) + 1
        needed = 90 * (2 * next_level - 1)
        return exp, float(needed)

    def read_has_transmo(self) -> bool:
        """Return True if the player has the transmogrification chest unlocked."""
        if not self._player_table or not self.attached:
            self.read_player_hp()
        if not self._player_table:
            return True  # default to True when memory unavailable
        val = _tab_get_number(self._handle, self._player_table, "has_transmo")
        return val is not None and val > 0

    def read_visited_zones(self) -> set[str]:
        """Return set of zone short_names the player has visited."""
        if not self.attached:
            return set()
        h = self._handle
        gt = self._global_table
        game_tab = _tab_get_table(h, gt, "game")
        if game_tab is None:
            return set()
        visited_tab = _tab_get_table(h, game_tab, "visited_zones")
        if visited_tab is None:
            return set()
        node_ptr = _ru32(h, visited_tab + 20)
        hmask = _ru32(h, visited_tab + 28)
        if not node_ptr or hmask is None or not _is_heap(node_ptr):
            return set()
        total = (hmask + 1) * _NODE_SIZE
        if total > 16 * 1024 * 1024:
            return set()
        bulk = _rpm(h, node_ptr, total)
        if not bulk:
            return set()
        result: set[str] = set()
        for i in range(hmask + 1):
            off = i * _NODE_SIZE
            key_it = struct.unpack_from("<I", bulk, off + 12)[0]
            val_it = struct.unpack_from("<I", bulk, off + 4)[0]
            if key_it != _LJ_TSTR or val_it != _LJ_TTRUE:
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
            raw = _rpm(h, key_gcs + 16, slen)
            if not raw:
                continue
            try:
                result.add(raw.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        return result

    def read_unique_deaths(self) -> set[str]:
        """Return set of unique entity names in game.state.unique_death (boss kills)."""
        if not self.attached:
            return set()
        h = self._handle
        gt = self._global_table
        game_tab = _tab_get_table(h, gt, "game")
        if game_tab is None:
            return set()
        state_tab = _tab_get_table(h, game_tab, "state")
        if state_tab is None:
            return set()
        deaths_tab = _tab_get_table(h, state_tab, "unique_death")
        if deaths_tab is None:
            return set()
        node_ptr = _ru32(h, deaths_tab + 20)
        hmask = _ru32(h, deaths_tab + 28)
        if not node_ptr or hmask is None or not _is_heap(node_ptr):
            return set()
        total = (hmask + 1) * _NODE_SIZE
        if total > 16 * 1024 * 1024:
            return set()
        bulk = _rpm(h, node_ptr, total)
        if not bulk:
            return set()
        result: set[str] = set()
        for i in range(hmask + 1):
            off = i * _NODE_SIZE
            key_it = struct.unpack_from("<I", bulk, off + 12)[0]
            val_it = struct.unpack_from("<I", bulk, off + 4)[0]
            if key_it != _LJ_TSTR or val_it != _LJ_TTRUE:
                continue
            key_gcs = struct.unpack_from("<I", bulk, off + 8)[0]
            if not _is_heap(key_gcs):
                continue
            slen_b = _rpm(h, key_gcs + 12, 4)
            if not slen_b:
                continue
            slen = struct.unpack("<I", slen_b)[0]
            if slen == 0 or slen > 256:
                continue
            raw = _rpm(h, key_gcs + 16, slen)
            if not raw:
                continue
            try:
                result.add(raw.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        return result

    def read_current_zone(self) -> tuple[str, int, int] | None:
        """Return (short_name, current_floor, max_floors) or None."""
        if not self.attached:
            return None
        h = self._handle
        gt = self._global_table
        game_tab = _tab_get_table(h, gt, "game")
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

    def read_prodigies(self) -> list[str]:
        """Return display names of prodigies available to learn (stat req met, not yet taken).

        Returns an empty list when the character is below level 25, not attached,
        or when no prodigy slots can be inferred from memory.

        Availability is determined by cross-referencing the prodigy DB against
        ``game.player.talents`` (already-learned) and ``game.player.stats``
        (base stats vs the 50-point threshold).  Only non-hidden prodigies are
        included.
        """
        if not self._player_table or not self.attached:
            self.read_player_hp()
        if not self._player_table:
            return []

        h = self._handle
        pt = self._player_table

        # Level gate: prodigies unlock at 25
        level = _tab_get_number(h, pt, "level")
        if level is None or level < 25:
            return []

        # Read base stats from game.player.stats sub-table
        stats_tab = _tab_get_table(h, pt, "stats")
        if stats_tab is None:
            return []
        player_stats: dict[str, float] = {}
        for stat in ("str", "dex", "con", "mag", "wil", "cun"):
            v = _tab_get_number(h, stats_tab, stat)
            player_stats[stat] = v if v is not None else 0.0

        # Read learned talents from game.player.talents
        talents_tab = _tab_get_table(h, pt, "talents")
        learned_ids: set[str] = set()
        if talents_tab is not None:
            for tid in _get_prodigy_db():
                node = _tab_find_strkey(h, talents_tab, tid)
                if node is None:
                    continue
                it = _ru32(h, node + 4)
                if it is None or it >= _LJ_TNUMX:
                    continue
                raw = _rpm(h, node, 8)
                if raw:
                    try:
                        if struct.unpack("<d", raw)[0] >= 1:
                            learned_ids.add(tid)
                    except struct.error:
                        pass

        # Available = stat req met (≥50 base) AND not yet learned
        available: list[str] = []
        for tid, (name, stat_key) in _get_prodigy_db().items():
            if tid in learned_ids:
                continue
            if player_stats.get(stat_key, 0.0) >= 50.0:
                available.append(name)

        return sorted(available)


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
